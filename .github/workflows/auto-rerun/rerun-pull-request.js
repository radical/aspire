// Pull request force-mode and broad transient-failure policy.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execSync } = require('node:child_process');
const common = require('./common.js');
const { createJobReader } = require('./github.js');
const maxTestOutputLength = 10 * 1024; // Cap test output before configurable regex matching.
const {
    failureConclusions, ignoredJobs, defaultMaxRetryableJobs, defaultMaxRunAttempt,
    getFailedSteps, toAnnotationText, matchesAny, findMatchingPattern,
    postTestCleanupFailureStepPatterns, windowsProcessInitializationFailurePatterns,
    formatMatchedPatternForMarkdown,
} = common;

const retryableWithAnnotationStepPatterns = [
    /^Set up job$/i,
    /^Checkout code$/i,
    /^Set up \.NET Core$/i,
    /^Install sdk for nuget based testing$/i,
    /^Upload logs, and test results$/i,
];

const ignoredFailureStepPatterns = [
    /^Run tests\b/i,
    /^Run nuget dependent tests\b/i,
    /^Build test project$/i,
    /^Build and archive test project$/i,
    /^Build RID-specific packages\b/i,
    /^Build Python validation image$/i,
    /^Build with packages$/i,
    /^Run .*SDK validation$/i,
    /^Check validation results$/i,
    /^Generate test results summary$/i,
    /^Copy CLI E2E recordings for upload$/i,
    /^Upload CLI E2E recordings$/i,
    /^Post Checkout code$/i,
    /^Install dependencies$/i,
];

const testExecutionFailureStepPatterns = [
    /^Run tests\b/i,
    /^Run nuget dependent tests\b/i,
];

const transientAnnotationPatterns = [
    /The job was not acquired by Runner of type hosted even after multiple attempts/i,
    /The hosted runner lost communication with the server/i,
    /Failed to resolve action download info/i,
    /Failed to CreateArtifact: Unable to make request: ENOTFOUND/i,
    /\bENOTFOUND\b/i,
    /\bECONNRESET\b/i,
    /\bEPROTO\b/i,
    /\bBad Gateway\b/i,
    /\bCould not resolve host\b/i,
    /\bSSL connection could not be established\b/i,
    /getaddrinfo ENOTFOUND builds\.dotnet\.microsoft\.com/i,
    /(timed out|failed to connect|could not resolve|ENOTFOUND|ECONNRESET|EPROTO).{0,120}builds\.dotnet\.microsoft\.com/i,
    /builds\.dotnet\.microsoft\.com.{0,120}(timed out|failed to connect|could not resolve|ENOTFOUND|ECONNRESET|EPROTO)/i,
    /(timed out|failed to connect|failed to respond|ENOTFOUND|ECONNRESET|Bad Gateway|SSL connection could not be established).{0,120}api\.github\.com/i,
    /api\.github\.com.{0,120}(timed out|failed to connect|failed to respond|ENOTFOUND|ECONNRESET|Bad Gateway|SSL connection could not be established)/i,
    /expected 'packfile'/i,
    /\bRPC failed\b/i,
    /\bRecv failure\b/i,
    /Couldn't connect to server/i,
    /Failed to connect to github\.com port/i,
    /The requested URL returned error:\s*(502|503|504)/i,
];

const ignoredFailureStepOverridePatterns = [
    /The job was not acquired by Runner of type hosted even after multiple attempts/i,
    /The hosted runner lost communication with the server/i,
    /Failed to resolve action download info/i,
    /Failed to download action .*api\.github\.com.*(502|503|504|Bad Gateway)/i,
];

const infrastructureNetworkFailureLogOverridePatterns = [
    /Unable to load the service index for source https:\/\/(?:pkgs\.dev\.azure\.com\/dnceng|dnceng\.pkgs\.visualstudio\.com)\/public\/_packaging\//i,
    /(timed out|failed to connect|could not resolve|ENOTFOUND|ECONNRESET|EPROTO|Bad Gateway|SSL connection could not be established).{0,160}https:\/\/(?:pkgs\.dev\.azure\.com\/dnceng|dnceng\.pkgs\.visualstudio\.com)\/public\/_packaging\//i,
    /https:\/\/(?:pkgs\.dev\.azure\.com\/dnceng|dnceng\.pkgs\.visualstudio\.com)\/public\/_packaging\/.{0,160}(timed out|failed to connect|could not resolve|ENOTFOUND|ECONNRESET|EPROTO|Bad Gateway|SSL connection could not be established)/i,
    /(timed out|failed to connect|could not resolve|ENOTFOUND|ECONNRESET|EPROTO|Bad Gateway|SSL connection could not be established).{0,160}builds\.dotnet\.microsoft\.com/i,
    /builds\.dotnet\.microsoft\.com.{0,160}(timed out|failed to connect|could not resolve|ENOTFOUND|ECONNRESET|EPROTO|Bad Gateway|SSL connection could not be established)/i,
    /(timed out|failed to connect|failed to respond|could not resolve|ENOTFOUND|ECONNRESET|EPROTO|Bad Gateway|SSL connection could not be established).{0,160}api\.github\.com/i,
    /api\.github\.com.{0,160}(timed out|failed to connect|failed to respond|could not resolve|ENOTFOUND|ECONNRESET|EPROTO|Bad Gateway|SSL connection could not be established)/i,
    /fatal: unable to access 'https:\/\/github\.com\/.*': The requested URL returned error:\s*(502|503|504)/i,
    /Failed to connect to github\.com port/i,
    /expected 'packfile'/i,
    /\bRPC failed\b/i,
    /\bRecv failure\b/i,
    /Unable to read data from the transport connection: Connection reset by peer/i,
];

function getFailureStepSignals(failedSteps) {
    const hasRetryableStep = failedSteps.some(step => matchesAny(step, retryableWithAnnotationStepPatterns));
    const hasIgnoredFailureStep = failedSteps.some(step => matchesAny(step, ignoredFailureStepPatterns));

    return {
        hasRetryableStep,
        hasIgnoredFailureStep,
        shouldInspectAnnotations: failedSteps.length === 0 || hasRetryableStep || hasIgnoredFailureStep,
    };
}

function canUseInfrastructureNetworkLogOverride(failedSteps) {
    return failedSteps.length > 0 && !failedSteps.some(step => matchesAny(step, testExecutionFailureStepPatterns));
}

function hasTestExecutionFailureStep(failedSteps) {
    return failedSteps.some(step => matchesAny(step, testExecutionFailureStepPatterns));
}

function formatFailedStepLabel(failedSteps, failedStepText) {
    const label = failedSteps.length === 1 ? 'Failed step' : 'Failed steps';
    return `${label} '${failedStepText}'`;
}

function isSingleFailedStep(failedSteps) {
    return failedSteps.length === 1;
}

function findInfrastructureNetworkLogOverridePattern(jobLogText) {
    return findMatchingPattern(jobLogText, infrastructureNetworkFailureLogOverridePatterns);
}

function getInfrastructureNetworkLogOverrideReason(failedSteps, failedStepText, matchedPattern) {
    const patternText = formatMatchedPatternForMarkdown(matchedPattern);
    return `${formatFailedStepLabel(failedSteps, failedStepText)} will be retried because the job log shows a likely transient infrastructure network failure.${patternText}`;
}

function getOutsideRetryRulesReason(failedSteps, failedStepText) {
    return `${formatFailedStepLabel(failedSteps, failedStepText)} ${isSingleFailedStep(failedSteps) ? 'is' : 'are'} not covered by the retry-safe rerun rules.`;
}

function getNoRetryMatchReason({
    failedSteps,
    failedStepText,
    hasRetryableStep,
    hasIgnoredFailureStep,
    hasTestExecutionFailureStep,
    annotationsText,
}) {
    const failedStepLabel = formatFailedStepLabel(failedSteps, failedStepText);

    if (hasTestExecutionFailureStep) {
        return `${failedStepLabel} ${isSingleFailedStep(failedSteps) ? 'includes' : 'include'} a test execution failure, so the job was not retried without a high-confidence infrastructure override.`;
    }

    if (hasIgnoredFailureStep) {
        return `${failedStepLabel} ${isSingleFailedStep(failedSteps) ? 'is' : 'are'} only retried when the job shows a high-confidence infrastructure override, and none was found.`;
    }

    if (hasRetryableStep) {
        return `${failedStepLabel} did not include a retry-safe transient infrastructure signal in the job annotations.`;
    }

    if (annotationsText) {
        return 'The job annotations did not show a retry-safe transient infrastructure failure.';
    }

    return 'No retry-safe transient infrastructure signal was found in the available job diagnostics.';
}

function classifyFailedJob(job, annotationsOrText, jobLogText = '', options = {}) {
    const {
        matchedInfrastructureNetworkLogOverridePattern: preMatchedInfrastructureNetworkLogOverridePattern,
    } = options;
    const failedSteps = getFailedSteps(job);
    const failedStepText = failedSteps.join(' | ');
    const { hasRetryableStep, hasIgnoredFailureStep, shouldInspectAnnotations } = getFailureStepSignals(failedSteps);
    const hasTestExecutionFailureStep = failedSteps.some(step => matchesAny(step, testExecutionFailureStepPatterns));
    const matchedInfrastructureNetworkLogOverridePattern =
        !hasTestExecutionFailureStep
            ? preMatchedInfrastructureNetworkLogOverridePattern === undefined
                ? findInfrastructureNetworkLogOverridePattern(jobLogText)
                : preMatchedInfrastructureNetworkLogOverridePattern
            : null;
    const matchesInfrastructureNetworkLogOverride = matchedInfrastructureNetworkLogOverridePattern !== null;

    if (!shouldInspectAnnotations) {
        if (matchesInfrastructureNetworkLogOverride) {
            return {
                retryable: true,
                failedSteps,
                reason: getInfrastructureNetworkLogOverrideReason(failedSteps, failedStepText, matchedInfrastructureNetworkLogOverridePattern),
            };
        }

        return {
            retryable: false,
            failedSteps,
            reason: getOutsideRetryRulesReason(failedSteps, failedStepText),
        };
    }

    const annotationsText = toAnnotationText(annotationsOrText);
    const matchesTransientAnnotation = matchesAny(annotationsText, transientAnnotationPatterns);
    const matchesIgnoredFailureStepOverride = matchesAny(annotationsText, ignoredFailureStepOverridePatterns);
    const hasOnlyPostTestCleanupFailures = failedSteps.length > 0
        && failedSteps.every(step => matchesAny(step, postTestCleanupFailureStepPatterns));
    const matchesWindowsProcessInitializationFailure = matchesAny(annotationsText, windowsProcessInitializationFailurePatterns);

    if (matchesTransientAnnotation && failedSteps.length === 0) {
        return {
            retryable: true,
            failedSteps,
            reason: 'Job-level runner or infrastructure failure matched the transient allowlist.',
        };
    }

    if (hasOnlyPostTestCleanupFailures && matchesWindowsProcessInitializationFailure) {
        return {
            retryable: true,
            failedSteps,
            reason: `Post-test cleanup steps '${failedStepText}' matched the Windows process initialization failure override allowlist.`,
        };
    }

    if (hasIgnoredFailureStep && matchesIgnoredFailureStepOverride) {
        return {
            retryable: true,
            failedSteps,
            reason: `Ignored failed step '${failedStepText}' matched the job-level infrastructure override allowlist.`,
        };
    }

    if (hasRetryableStep && !hasIgnoredFailureStep && matchesTransientAnnotation) {
        return {
            retryable: true,
            failedSteps,
            reason: `Failed step '${failedStepText}' matched the transient annotation allowlist.`,
        };
    }

    if (matchesInfrastructureNetworkLogOverride) {
        return {
            retryable: true,
            failedSteps,
            reason: getInfrastructureNetworkLogOverrideReason(failedSteps, failedStepText, matchedInfrastructureNetworkLogOverridePattern),
        };
    }

    return {
        retryable: false,
        failedSteps,
        reason: getNoRetryMatchReason({
            failedSteps,
            failedStepText,
            hasRetryableStep,
            hasIgnoredFailureStep,
            hasTestExecutionFailureStep,
            annotationsText,
        }),
    };
}

async function analyzeFailedJobs({
    jobs,
    getAnnotationsForJob,
    getJobLogTextForJob,
    maxRetryableJobs = defaultMaxRetryableJobs,
    retryPatternsConfig = null,
}) {
    const normalizedMaxRetryableJobs =
        Number.isInteger(maxRetryableJobs) && maxRetryableJobs >= 0
            ? maxRetryableJobs
            : defaultMaxRetryableJobs;
    const failedJobs = (jobs || []).filter(job => failureConclusions.has(job.conclusion) && !ignoredJobs.has(job.name));
    const retryableJobs = [];
    const skippedJobs = [];
    const jobFailurePatterns = retryPatternsConfig?.jobFailurePatterns;

    for (const job of failedJobs) {
        const failedSteps = getFailedSteps(job);
        const { shouldInspectAnnotations } = getFailureStepSignals(failedSteps);
        const annotations = shouldInspectAnnotations && getAnnotationsForJob
            ? await getAnnotationsForJob(job)
            : '';
        let classification = classifyFailedJob(
            job,
            annotations
        );

        const shouldInspectLogs =
            !classification.retryable &&
            getJobLogTextForJob &&
            canUseInfrastructureNetworkLogOverride(failedSteps) &&
            normalizedMaxRetryableJobs > 0 &&
            retryableJobs.length <= normalizedMaxRetryableJobs;

        if (shouldInspectLogs) {
            const jobLogText = await getJobLogTextForJob(job);
            const matchedInfrastructureNetworkLogOverridePattern = findInfrastructureNetworkLogOverridePattern(jobLogText);
            classification = classifyFailedJob(
                job,
                annotations,
                jobLogText,
                { matchedInfrastructureNetworkLogOverridePattern }
            );
        }

        // Third classification pass: configurable job log patterns for test execution failures
        if (
            !classification.retryable &&
            getJobLogTextForJob &&
            Array.isArray(jobFailurePatterns) &&
            jobFailurePatterns.length > 0 &&
            hasTestExecutionFailureStep(failedSteps)
        ) {
            const jobLogText = await getJobLogTextForJob(job);
            const match = matchJobLogPattern(job.name, jobLogText, jobFailurePatterns);

            if (match) {
                const failedStepText = failedSteps.join(' | ');
                classification = {
                    retryable: true,
                    failedSteps,
                    reason: `${formatFailedStepLabel(failedSteps, failedStepText)} will be retried because the job log matched a configurable test-retry pattern: ${match.reason}`,
                };
            }
        }

        const jobResult = {
            id: job.id,
            name: job.name,
            htmlUrl: job.html_url || null,
            failedSteps: classification.failedSteps,
            reason: classification.reason,
        };

        if (classification.retryable) {
            retryableJobs.push(jobResult);
        }
        else {
            skippedJobs.push(jobResult);
        }
    }

    return { failedJobs, retryableJobs, skippedJobs };
}

function loadRetryPatternsConfig(configPath) {
    try {
        const content = fs.readFileSync(configPath, 'utf8');
        const config = JSON.parse(content);
        const validation = validateRetryPatternsConfig(config);

        if (!validation.valid) {
            return { config: null, errors: validation.errors };
        }

        const warnings = compileRetryPatterns(config);

        return { config, errors: [], warnings };
    }
    catch (error) {
        return { config: null, errors: [`Failed to load config: ${error.message}`] };
    }
}

function compileRetryPatterns(config) {
    const warnings = [];
    const allRules = [
        ...(config.testFailurePatterns || []),
        ...(config.jobFailurePatterns || []),
    ];

    for (const rule of allRules) {
        for (const value of Object.values(rule)) {
            if (value && typeof value === 'object' && typeof value.regex === 'string') {
                try {
                    value._compiledRegex = new RegExp(value.regex, 'i');
                }
                catch (error) {
                    warnings.push(`Invalid regex '${value.regex}': ${error.message} — rule will be skipped.`);
                    rule.enabled = false;
                }
            }
        }
    }

    return warnings;
}

function validateRetryPatternsConfig(config) {
    const errors = [];

    if (!config || typeof config !== 'object' || Array.isArray(config)) {
        errors.push('Config must be a non-null object.');
        return { valid: false, errors };
    }

    const allowedTopLevel = new Set(['version', 'testFailurePatterns', 'jobFailurePatterns']);
    for (const key of Object.keys(config)) {
        if (!allowedTopLevel.has(key)) {
            errors.push(`Unknown top-level property '${key}'.`);
        }
    }

    if (config.version !== 1) {
        errors.push(`Expected version 1, got ${JSON.stringify(config.version)}.`);
    }

    const testPatternAllowedFields = new Set(['testName', 'testProject', 'output', 'reason', 'enabled']);
    const jobPatternAllowedFields = new Set(['jobName', 'output', 'reason', 'enabled']);
    const testPatternMatcherFields = ['testName', 'testProject', 'output'];
    const jobPatternMatcherFields = ['jobName', 'output'];

    if (config.testFailurePatterns !== undefined) {
        if (!Array.isArray(config.testFailurePatterns)) {
            errors.push('testFailurePatterns must be an array.');
        }
        else {
            config.testFailurePatterns.forEach((rule, i) => {
                validatePatternRule(rule, `testFailurePatterns[${i}]`, testPatternAllowedFields, testPatternMatcherFields, errors);
            });
        }
    }

    if (config.jobFailurePatterns !== undefined) {
        if (!Array.isArray(config.jobFailurePatterns)) {
            errors.push('jobFailurePatterns must be an array.');
        }
        else {
            config.jobFailurePatterns.forEach((rule, i) => {
                validatePatternRule(rule, `jobFailurePatterns[${i}]`, jobPatternAllowedFields, jobPatternMatcherFields, errors);
            });
        }
    }

    return { valid: errors.length === 0, errors };
}

function validatePatternRule(rule, path, allowedFields, matcherFields, errors) {
    if (!rule || typeof rule !== 'object' || Array.isArray(rule)) {
        errors.push(`${path}: must be a non-null object.`);
        return;
    }

    for (const key of Object.keys(rule)) {
        if (!allowedFields.has(key)) {
            errors.push(`${path}: unknown field '${key}'.`);
        }
    }

    if (typeof rule.reason !== 'string' || rule.reason.trim().length === 0) {
        errors.push(`${path}: 'reason' must be a non-empty string.`);
    }

    if (rule.enabled !== undefined && typeof rule.enabled !== 'boolean') {
        errors.push(`${path}: 'enabled' must be a boolean.`);
    }

    const hasMatcherField = matcherFields.some(field => rule[field] !== undefined);
    if (!hasMatcherField) {
        errors.push(`${path}: must contain at least one matcher field (${matcherFields.join(', ')}).`);
    }

    for (const field of matcherFields) {
        if (rule[field] !== undefined) {
            validatePatternValue(rule[field], `${path}.${field}`, errors);
        }
    }
}

function validatePatternValue(value, path, errors) {
    if (typeof value === 'string') {
        if (value.length === 0) {
            errors.push(`${path}: string pattern must be non-empty.`);
        }
        return;
    }

    if (value && typeof value === 'object' && !Array.isArray(value)) {
        if (typeof value.regex !== 'string' || value.regex.length === 0) {
            errors.push(`${path}: regex pattern must have a non-empty 'regex' string.`);
            return;
        }

        const allowedRegexKeys = new Set(['regex']);
        for (const key of Object.keys(value)) {
            if (!allowedRegexKeys.has(key)) {
                errors.push(`${path}: unknown regex property '${key}'.`);
            }
        }

        try {
            new RegExp(value.regex, 'i');
        }
        catch (regexError) {
            errors.push(`${path}: invalid regex '${value.regex}': ${regexError.message}`);
        }

        return;
    }

    errors.push(`${path}: must be a string or { "regex": "..." } object.`);
}

function matchesRetryPattern(text, patternValue) {
    if (!text || !patternValue) {
        return false;
    }

    if (typeof patternValue === 'string') {
        return text.toLowerCase().includes(patternValue.toLowerCase());
    }

    if (patternValue && typeof patternValue === 'object') {
        if (patternValue._compiledRegex) {
            return patternValue._compiledRegex.test(text);
        }

        if (typeof patternValue.regex === 'string') {
            try {
                return new RegExp(patternValue.regex, 'i').test(text);
            }
            catch {
                return false;
            }
        }
    }

    return false;
}

function isPatternEnabled(rule) {
    return rule.enabled !== false;
}

function matchTestFailurePatterns(failedTests, testProject, patterns) {
    if (!Array.isArray(failedTests) || failedTests.length === 0 || !Array.isArray(patterns) || patterns.length === 0) {
        return { shouldRetry: false, matchedTests: [] };
    }

    const enabledPatterns = patterns.filter(isPatternEnabled);
    if (enabledPatterns.length === 0) {
        return { shouldRetry: false, matchedTests: [] };
    }

    const matchedTests = [];

    for (const test of failedTests) {
        for (const pattern of enabledPatterns) {
            if (matchesSingleTestPattern(test, testProject, pattern)) {
                const matchedSnippet = extractMatchedSnippet(test.output, pattern.output);
                matchedTests.push({
                    testName: test.testName,
                    reason: pattern.reason,
                    matchedSnippet,
                });
                break; // first matching rule wins per test
            }
        }
    }

    return { shouldRetry: matchedTests.length > 0, matchedTests };
}

function matchesSingleTestPattern(test, testProject, pattern) {
    if (pattern.testName !== undefined && !matchesRetryPattern(test.testName, pattern.testName)) {
        return false;
    }

    if (pattern.testProject !== undefined && !matchesRetryPattern(testProject, pattern.testProject)) {
        return false;
    }

    if (pattern.output !== undefined && !matchesRetryPattern(test.output, pattern.output)) {
        return false;
    }

    return true;
}

function extractMatchedSnippet(output, patternOutput) {
    if (!output || !patternOutput) {
        return '';
    }

    const maxSnippetLength = 200;
    const searchTerm = typeof patternOutput === 'string' ? patternOutput : null;

    if (searchTerm) {
        const lowerOutput = output.toLowerCase();
        const lowerSearch = searchTerm.toLowerCase();
        const index = lowerOutput.indexOf(lowerSearch);

        if (index >= 0) {
            const start = Math.max(0, index - 40);
            const end = Math.min(output.length, index + searchTerm.length + 40);
            let snippet = output.slice(start, end);

            if (start > 0) {
                snippet = '...' + snippet;
            }

            if (end < output.length) {
                snippet = snippet + '...';
            }

            return snippet.length > maxSnippetLength
                ? snippet.slice(0, maxSnippetLength - 3) + '...'
                : snippet;
        }
    }

    return output.length > maxSnippetLength
        ? output.slice(0, maxSnippetLength - 3) + '...'
        : output;
}

function matchJobLogPattern(jobName, jobLogText, patterns) {
    if (!Array.isArray(patterns) || patterns.length === 0) {
        return null;
    }

    const enabledPatterns = patterns.filter(isPatternEnabled);

    for (const pattern of enabledPatterns) {
        if (pattern.jobName !== undefined && !matchesRetryPattern(jobName, pattern.jobName)) {
            continue;
        }

        if (pattern.output !== undefined && !matchesRetryPattern(jobLogText, pattern.output)) {
            continue;
        }

        return { matched: true, reason: pattern.reason };
    }

    return null;
}

function extractFailedTestsFromTrx(trxContent) {
    if (!trxContent || typeof trxContent !== 'string') {
        return [];
    }

    const failedTests = [];

    // Match UnitTestResult elements with outcome="Failed" (handles attribute order variation)
    const resultRegex = /<UnitTestResult\b[^>]*\boutcome\s*=\s*"Failed"[^>]*>[\s\S]*?<\/UnitTestResult>/gi;
    let resultMatch;

    while ((resultMatch = resultRegex.exec(trxContent)) !== null) {
        const block = resultMatch[0];
        const testNameMatch = block.match(/\btestName\s*=\s*"([^"]*)"/i);

        if (!testNameMatch) {
            continue;
        }

        const testName = decodeXmlEntities(testNameMatch[1]);
        const message = extractXmlElementContent(block, 'Message');
        const stackTrace = extractXmlElementContent(block, 'StackTrace');
        const stdOut = extractXmlElementContent(block, 'StdOut');

        let output = [message, stackTrace, stdOut].filter(Boolean).join('\n');

        if (output.length > maxTestOutputLength) {
            output = output.slice(0, maxTestOutputLength);
        }

        failedTests.push({ testName, output });
    }

    return failedTests;
}

function extractXmlElementContent(xml, elementName) {
    const regex = new RegExp(`<${elementName}>([\\s\\S]*?)</${elementName}>`, 'i');
    const match = xml.match(regex);
    return match ? decodeXmlEntities(match[1].trim()) : '';
}

function decodeXmlEntities(text) {
    if (!text) {
        return '';
    }

    return text
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&apos;/g, "'")
        .replace(/&amp;/g, '&');
}

function analyzeTrxFiles(trxFileContents, testFailurePatterns) {
    if (!Array.isArray(trxFileContents) || trxFileContents.length === 0 || !Array.isArray(testFailurePatterns) || testFailurePatterns.length === 0) {
        return { allMatchedTests: [] };
    }

    const allMatchedTests = [];
    const seenTestNames = new Set();

    for (const { fileName, content } of trxFileContents) {
        const failedTests = extractFailedTestsFromTrx(content);
        const testProject = String(fileName || '').replace(/\.trx$/i, '');
        const { matchedTests } = matchTestFailurePatterns(failedTests, testProject, testFailurePatterns);

        for (const match of matchedTests) {
            if (!seenTestNames.has(match.testName)) {
                seenTestNames.add(match.testName);
                allMatchedTests.push({ ...match, testProject });
            }
        }
    }

    return { allMatchedTests };
}

function promoteTestExecutionFailureJobs(retryableJobs, skippedJobs, allMatchedTests) {
    if (!Array.isArray(allMatchedTests) || allMatchedTests.length === 0) {
        return { retryableJobs, skippedJobs, promotedJobs: [] };
    }

    const matchSummary = [...new Set(allMatchedTests.map(m => m.reason))].join(', ');
    const promotedJobs = [];
    const remainingSkippedJobs = [];

    for (const job of skippedJobs) {
        if (hasTestExecutionFailureStep(job.failedSteps)) {
            promotedJobs.push({
                ...job,
                reason: `Test execution failure will be retried because ${allMatchedTests.length} failed test(s) matched transient test failure patterns (${matchSummary}).`,
                matchedTests: allMatchedTests,
            });
        }
        else {
            remainingSkippedJobs.push(job);
        }
    }

    return {
        retryableJobs: [...retryableJobs, ...promotedJobs],
        skippedJobs: remainingSkippedJobs,
        promotedJobs,
    };
}

function selectTestResultsArtifact(artifacts) {
    if (!Array.isArray(artifacts) || artifacts.length === 0) {
        return null;
    }

    const maxArtifactBytes = 100 * 1024 * 1024; // 100MB cap
    const candidates = artifacts
        .filter(a => a.name === 'All-TestResults' && !a.expired)
        .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

    if (candidates.length === 0) {
        return null;
    }

    const selected = candidates[0];

    if (selected.size_in_bytes > maxArtifactBytes) {
        return null;
    }

    return selected;
}

async function analyzePullRequestFailures({
    github, core, owner, repo, workflowRun, eventName, dryRun, forceRerunAll, workspace, token,
}) {
    const rerunWorkflow = { ...common, ...module.exports };
    const isWorkflowDispatch = eventName === 'workflow_dispatch';
    const maxRunAttempt = defaultMaxRunAttempt;
    const maxRetryableJobs = defaultMaxRetryableJobs;
    const { paginate, listJobsForAttempt, listAnnotations, getJobLogText } =
        createJobReader({ github, owner, repo, core, token });
    const outputs = {};
    const setOutput = (name, value) => { outputs[name] = value; };

    async function analyze() {
        const sourceRunUrl = workflowRun.html_url || `https://github.com/${owner}/${repo}/actions/runs/${workflowRun.id}`;

        setOutput('source_run_id', String(workflowRun.id));
        setOutput('source_run_attempt', String(workflowRun.run_attempt || ''));
        setOutput('source_run_url', sourceRunUrl);
        setOutput('dry_run', String(dryRun));
        setOutput('max_retryable_jobs', String(maxRetryableJobs));
        setOutput('retryable_jobs', '[]');
        setOutput('pull_request_numbers', '[]');
        setOutput('retryable_count', '0');
        setOutput('skipped_count', '0');
        setOutput('rerun_eligible', 'false');
        setOutput('rerun_execution_eligible', 'false');
        setOutput('test_pattern_matched_tests', '[]');

        if (workflowRun.name && workflowRun.name !== 'CI') {
            core.info(`Workflow run ${workflowRun.id} is '${workflowRun.name}', not 'CI'. Skipping.`);
            return;
        }

        if (!isWorkflowDispatch && !rerunWorkflow.computeRerunEligibility({
            runAttempt: workflowRun.run_attempt,
            maxRunAttempt,
            forceRerunAll: true,
        })) {
            const message =
                `Automatic rerun attempt cap reached at source attempt ${workflowRun.run_attempt}; ` +
                `the configured cap is ${maxRunAttempt}. No jobs were inspected or rerun.`;
            core.info(message);
            await rerunWorkflow.writeRerunOutcomeSummary({
                summary: core.summary,
                sourceRunUrl,
                sourceRunAttempt: workflowRun.run_attempt,
                message,
            });
            return;
        }

        const pullRequestNumbers = await rerunWorkflow.getAssociatedPullRequestNumbers({
            github,
            owner,
            repo,
            workflowRun,
            warn: message => core.warning(message),
        });
        setOutput('pull_request_numbers', JSON.stringify(pullRequestNumbers));

        // The open-PR requirement applies in all modes: skip runs with no
        // associated PR. Force mode does not bypass this — there is no value in
        // spending CI on a run that has no open PR behind it.
        if (pullRequestNumbers.length === 0) {
            const message = 'No associated pull request could be resolved for this workflow run. No jobs were rerun.';
            core.info(message);
            await rerunWorkflow.writeRerunOutcomeSummary({
                summary: core.summary,
                sourceRunUrl,
                sourceRunAttempt: workflowRun.run_attempt,
                message,
            });
            return;
        }

        // TEMPORARY — FORCE_RERUN_ALL short-circuit (revert when no longer needed):
        // The run failed (job-level `if`) and has an associated PR (checked above),
        // so request a rerun without fetching or classifying any jobs. Only the
        // attempt cap is re-checked here. The retryable_jobs output
        // stays '[]' on purpose: the rerun job uses GitHub's rerun-failed-jobs API,
        // which reruns every failed job regardless of this list, and the final
        // open-PR state is re-checked there. See the JS file-level comment.
        if (forceRerunAll) {
            const forceRunAttempt = workflowRun.run_attempt;
            const forceRerunEligible = rerunWorkflow.computeRerunEligibility({
                runAttempt: forceRunAttempt,
                forceRerunAll: true,
            });
            const forceRerunExecutionEligible = rerunWorkflow.computeRerunExecutionEligibility({
                dryRun,
                runAttempt: forceRunAttempt,
                forceRerunAll: true,
            });

            setOutput('rerun_eligible', String(forceRerunEligible));
            setOutput('rerun_execution_eligible', String(forceRerunExecutionEligible));

            await rerunWorkflow.writeForceRerunSummary({
                summary: core.summary,
                rerunEligible: forceRerunEligible,
                dryRun,
                sourceRunUrl,
                sourceRunAttempt: forceRunAttempt,
                runAttempt: forceRunAttempt,
                openPullRequestNumbers: pullRequestNumbers,
            });

            if (!forceRerunEligible) {
                core.info(`Force-rerun mode: attempt cap reached (attempt ${forceRunAttempt}). Skipping.`);
            }

            return;
        }

        const runId = workflowRun.id;
        const runAttempt = workflowRun.run_attempt;
        const jobs = await listJobsForAttempt(runId, runAttempt);

        // Load test retry patterns config
        const configPath = path.join(workspace, 'eng', 'test-retry-patterns.json');
        const { config: retryPatternsConfig, errors: configErrors } = rerunWorkflow.loadRetryPatternsConfig(configPath);
        if (configErrors.length > 0) {
            core.warning(`Test retry patterns config has errors: ${configErrors.join('; ')}`);
        }

        let { failedJobs, retryableJobs, skippedJobs } = await rerunWorkflow.analyzeFailedJobs({
            jobs,
            getAnnotationsForJob: async job => listAnnotations(job),
            getJobLogTextForJob: async job => getJobLogText(job.id),
            maxRetryableJobs,
            retryPatternsConfig,
        });

        // TRX-based analysis: check test output for transient patterns.
        let testPatternMatchedTests = [];
        const hasSkippedTestExecJobs = skippedJobs.some(job =>
            rerunWorkflow.hasTestExecutionFailureStep(job.failedSteps)
        );
        const testFailurePatterns = retryPatternsConfig?.testFailurePatterns;

        if (hasSkippedTestExecJobs && Array.isArray(testFailurePatterns) && testFailurePatterns.length > 0) {
            try {
                const artifacts = await paginate(
                    'GET /repos/{owner}/{repo}/actions/runs/{run_id}/artifacts',
                    { owner, repo, run_id: runId },
                    data => data.artifacts || []);
                const testArtifact = rerunWorkflow.selectTestResultsArtifact(artifacts);

                if (testArtifact) {
                    core.info(`Downloading test results artifact '${testArtifact.name}' (${testArtifact.size_in_bytes} bytes)...`);
                    const download = await github.rest.actions.downloadArtifact({
                        owner,
                        repo,
                        artifact_id: testArtifact.id,
                        archive_format: 'zip',
                    });

                    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'test-results-'));
                    try {
                        const zipPath = path.join(tmpDir, 'test-results.zip');
                        fs.writeFileSync(zipPath, Buffer.from(download.data));
                        const trxDir = path.join(tmpDir, 'trx');
                        fs.mkdirSync(trxDir, { recursive: true });
                        execSync(`unzip -qo "${zipPath}" -d "${trxDir}"`, { timeout: 30_000 });

                        const trxFileContents = [];
                        const maxTrxFiles = 200;
                        const maxTrxFileBytes = 50 * 1024 * 1024; // 50MB per file cap
                        const resolvedTrxDir = fs.realpathSync(trxDir);
                        const findTrxFiles = (dir) => {
                            for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
                                if (entry.isSymbolicLink()) {
                                    continue;
                                }
                                const fullPath = path.join(dir, entry.name);
                                const resolvedPath = fs.realpathSync(fullPath);
                                if (!resolvedPath.startsWith(resolvedTrxDir + path.sep) && resolvedPath !== resolvedTrxDir) {
                                    continue;
                                }
                                if (entry.isDirectory()) {
                                    findTrxFiles(fullPath);
                                } else if (entry.name.endsWith('.trx') && trxFileContents.length < maxTrxFiles) {
                                    const stat = fs.statSync(fullPath);
                                    if (stat.size <= maxTrxFileBytes) {
                                        trxFileContents.push({
                                            fileName: entry.name,
                                            content: fs.readFileSync(fullPath, 'utf8'),
                                        });
                                    }
                                }
                            }
                        };
                        findTrxFiles(trxDir);

                        if (trxFileContents.length > 0) {
                            const { allMatchedTests } = rerunWorkflow.analyzeTrxFiles(trxFileContents, testFailurePatterns);
                            if (allMatchedTests.length > 0) {
                                core.info(`Found ${allMatchedTests.length} test(s) matching transient failure patterns.`);
                                const promoted = rerunWorkflow.promoteTestExecutionFailureJobs(retryableJobs, skippedJobs, allMatchedTests);
                                retryableJobs = promoted.retryableJobs;
                                skippedJobs = promoted.skippedJobs;
                                testPatternMatchedTests = allMatchedTests;
                            }
                        }
                    } finally {
                        fs.rmSync(tmpDir, { recursive: true, force: true });
                    }
                }
            } catch (trxError) {
                core.warning(`TRX analysis failed (non-fatal): ${trxError.message}`);
            }
        }

        setOutput('retryable_jobs', JSON.stringify(retryableJobs.map(job => ({
            id: job.id,
            name: job.name,
            htmlUrl: job.htmlUrl,
            reason: job.reason,
        }))));
        setOutput('retryable_count', String(retryableJobs.length));
        setOutput('skipped_count', String(skippedJobs.length));

        const rerunEligible = rerunWorkflow.computeRerunEligibility({
            retryableCount: retryableJobs.length,
            maxRetryableJobs,
            runAttempt,
        });
        const rerunExecutionEligible = rerunWorkflow.computeRerunExecutionEligibility({
            dryRun,
            retryableCount: retryableJobs.length,
            maxRetryableJobs,
            runAttempt,
        });
        setOutput('rerun_eligible', String(rerunEligible));
        setOutput('rerun_execution_eligible', String(rerunExecutionEligible));
        setOutput('test_pattern_matched_tests', JSON.stringify(testPatternMatchedTests.slice(0, 50).map(t => ({
            testName: t.testName,
            reason: t.reason,
        }))));

        await rerunWorkflow.writeAnalysisSummary({
            summary: core.summary,
            failedJobs,
            retryableJobs,
            skippedJobs,
            maxRetryableJobs,
            dryRun,
            rerunEligible,
            sourceRunUrl,
            sourceRunAttempt: runAttempt,
            testPatternMatchedTests,
        });

        if (retryableJobs.length === 0) {
            core.info('No retryable failed jobs were detected.');
            return;
        }

    }
    await analyze();
    return {
        run: {
            ...workflowRun,
            html_url: workflowRun.html_url || `https://github.com/${owner}/${repo}/actions/runs/${workflowRun.id}`,
        },
        policy: 'pull-request',
        forceRerunAll,
        dryRun,
        executionEligible: outputs.rerun_execution_eligible === 'true',
        retryableJobs: JSON.parse(outputs.retryable_jobs || '[]'),
        pullRequestNumbers: JSON.parse(outputs.pull_request_numbers || '[]'),
        testPatternMatchedTests: JSON.parse(outputs.test_pattern_matched_tests || '[]'),
    };
}

async function rerunPullRequestFailures(options) {
    return common.requestFailedJobsRerun({
        ...options,
        revalidateSourceAttempt: true,
    });
}

module.exports = {
    testExecutionFailureStepPatterns,
    getFailureStepSignals,
    canUseInfrastructureNetworkLogOverride,
    hasTestExecutionFailureStep,
    formatFailedStepLabel,
    isSingleFailedStep,
    findInfrastructureNetworkLogOverridePattern,
    getInfrastructureNetworkLogOverrideReason,
    getOutsideRetryRulesReason,
    getNoRetryMatchReason,
    classifyFailedJob,
    analyzeFailedJobs,
    loadRetryPatternsConfig,
    compileRetryPatterns,
    validateRetryPatternsConfig,
    validatePatternRule,
    validatePatternValue,
    matchesRetryPattern,
    isPatternEnabled,
    matchTestFailurePatterns,
    matchesSingleTestPattern,
    extractMatchedSnippet,
    matchJobLogPattern,
    extractFailedTestsFromTrx,
    extractXmlElementContent,
    decodeXmlEntities,
    analyzeTrxFiles,
    promoteTestExecutionFailureJobs,
    selectTestResultsArtifact,
    analyzePullRequestFailures,
    rerunPullRequestFailures,
};
