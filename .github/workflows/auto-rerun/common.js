// Shared diagnostics, GitHub rerun request, and reporting helpers.
const failureConclusions = new Set(['failure', 'cancelled', 'timed_out', 'startup_failure']);
const ignoredJobs = new Set(['Final Results', 'Tests / Final Test Results']);
const defaultMaxRetryableJobs = 5;
const defaultMaxRunAttempt = 3;
const defaultDelay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

const postTestCleanupFailureStepPatterns = [
    /^Upload logs, and test results$/i,
    /^Copy CLI E2E recordings for upload$/i,
    /^Upload CLI E2E recordings$/i,
    /^Generate test results summary$/i,
    /^Post Checkout code$/i,
];

const windowsProcessInitializationFailurePatterns = [
    /Process completed with exit code -1073741502/i,
    /\b0xC0000142\b/i,
];

function matchesAny(value, patterns) {
    return patterns.some(pattern => pattern.test(value));
}

function findMatchingPattern(value, patterns) {
    return patterns.find(pattern => pattern.test(value)) ?? null;
}

function createBoundedDiagnosticPairPatterns(firstPattern, secondPatterns, maxDistance) {
    return secondPatterns.flatMap(secondPattern => [
        new RegExp(`${firstPattern.source}[\\s\\S]{0,${maxDistance}}${secondPattern.source}`, 'i'),
        new RegExp(`${secondPattern.source}[\\s\\S]{0,${maxDistance}}${firstPattern.source}`, 'i'),
    ]);
}

function parseCheckRunId(checkRunUrl) {
    if (typeof checkRunUrl !== 'string') {
        return null;
    }

    const match = checkRunUrl.match(/\/check-runs\/(\d+)(?:\/|$)/);
    if (!match) {
        return null;
    }

    const checkRunId = Number(match[1]);
    return Number.isInteger(checkRunId) && checkRunId > 0 ? checkRunId : null;
}

function getPullRequestNumbers(workflowRun) {
    return [...new Set((workflowRun?.pull_requests || [])
        .map(pullRequest => pullRequest.number)
        .filter(Number.isInteger))];
}

function getHeadRepositoryOwnerLogin(workflowRun) {
    return workflowRun?.head_repository?.owner?.login ?? workflowRun?.head_repository?.owner?.name ?? null;
}

function matchesWorkflowRunHead(pullRequest, workflowRun, headOwner, headBranch) {
    const pullRequestHead = pullRequest?.head;
    const pullRequestHeadOwner = pullRequestHead?.repo?.owner?.login ?? pullRequestHead?.user?.login ?? null;

    if (typeof pullRequestHeadOwner !== 'string' || pullRequestHeadOwner.toLowerCase() !== headOwner.toLowerCase()) {
        return false;
    }

    if (pullRequestHead?.ref !== headBranch) {
        return false;
    }

    const workflowHeadSha = workflowRun?.head_sha;
    const pullRequestHeadSha = pullRequestHead?.sha;

    return typeof workflowHeadSha !== 'string'
        || workflowHeadSha.length === 0
        || typeof pullRequestHeadSha !== 'string'
        || pullRequestHeadSha.length === 0
        || pullRequestHeadSha === workflowHeadSha;
}

async function listPullRequestsByHead({ github, owner, repo, head, warn }) {
    const pullRequests = [];

    try {
        for (let page = 1; ; page++) {
            const response = await github.request('GET /repos/{owner}/{repo}/pulls', {
                owner,
                repo,
                state: 'all',
                head,
                per_page: 100,
                page,
            });

            pullRequests.push(...(response.data || []));

            if (!response.headers?.link || !response.headers.link.includes('rel="next"')) {
                return pullRequests;
            }
        }
    }
    catch (error) {
        if (typeof warn === 'function') {
            warn(`Failed to resolve pull requests for head '${head}': ${error.message}`);
        }
        return [];
    }
}

async function getAssociatedPullRequestNumbers({ github, owner, repo, workflowRun, warn }) {
    const pullRequestNumbers = getPullRequestNumbers(workflowRun);
    if (pullRequestNumbers.length > 0) {
        return pullRequestNumbers;
    }

    const headOwner = getHeadRepositoryOwnerLogin(workflowRun);
    const headBranch = workflowRun?.head_branch;

    if (typeof headOwner !== 'string' || headOwner.length === 0 || typeof headBranch !== 'string' || headBranch.length === 0) {
        return [];
    }

    const responseData = await listPullRequestsByHead({
        github,
        owner,
        repo,
        head: `${headOwner}:${headBranch}`,
        warn,
    });

    const matchingPullRequests = responseData
        .filter(pullRequest => matchesWorkflowRunHead(pullRequest, workflowRun, headOwner, headBranch));
    const fallbackPullRequestNumbers = [...new Set(matchingPullRequests
        .map(pullRequest => pullRequest.number)
        .filter(Number.isInteger))];

    return fallbackPullRequestNumbers.length === 1 ? fallbackPullRequestNumbers : [];
}

async function getCheckRunIdForJob({ job, getJobForWorkflowRun }) {
    const checkRunIdFromJob = parseCheckRunId(job?.check_run_url);
    if (checkRunIdFromJob) {
        return checkRunIdFromJob;
    }

    if (!getJobForWorkflowRun || !Number.isInteger(job?.id) || job.id <= 0) {
        return null;
    }

    const workflowJob = await getJobForWorkflowRun(job.id);
    return parseCheckRunId(workflowJob?.check_run_url);
}

function getFailedSteps(job) {
    return (job.steps || [])
        .filter(step => failureConclusions.has(step.conclusion))
        .map(step => step.name);
}

function annotationText(annotations) {
    return (annotations || [])
        .flatMap(annotation => [annotation.title, annotation.message, annotation.raw_details].filter(Boolean))
        .join('\n');
}

function toAnnotationText(annotationsOrText) {
    if (!annotationsOrText) {
        return '';
    }

    if (typeof annotationsOrText === 'string') {
        return annotationsOrText;
    }

    return annotationText(annotationsOrText);
}

function computeRerunEligibility({
    retryableCount,
    maxRetryableJobs = defaultMaxRetryableJobs,
    runAttempt = 1,
    maxRunAttempt = defaultMaxRunAttempt,
    forceRerunAll = false
}) {
    // The attempt cap applies in every mode: never rerun past maxRunAttempt
    // failed source attempts.
    if (runAttempt > maxRunAttempt) {
        return false;
    }

    // FORCE_RERUN_ALL short-circuit: the run failed (YAML trigger gate) and has an open
    // PR (checked in the workflow), so it is eligible — only the attempt cap above
    // matters. There is no job-count analysis. See the file-level comment to disable.
    if (forceRerunAll) {
        return true;
    }

    if (retryableCount <= 0) {
        return false;
    }

    // For attempts after the first (runAttempt > 1) apply a stricter cap:
    // fewer than maxRetryableJobs jobs (i.e. strictly less than the cap rather
    // than less-than-or-equal).
    return runAttempt <= 1
        ? retryableCount <= maxRetryableJobs
        : retryableCount < maxRetryableJobs;
}

function computeRerunExecutionEligibility({
    dryRun,
    retryableCount,
    maxRetryableJobs = defaultMaxRetryableJobs,
    runAttempt = 1,
    maxRunAttempt = defaultMaxRunAttempt,
    forceRerunAll = false
}) {
    return !dryRun && computeRerunEligibility({ retryableCount, maxRetryableJobs, runAttempt, maxRunAttempt, forceRerunAll });
}

function buildSummaryReference(url, text) {
    return { url, text };
}

function addSummaryReference(summary, label, reference) {
    summary.addRaw(`${label}: `);

    if (reference?.url) {
        summary.addLink(reference.text, reference.url);
    }
    else {
        summary.addRaw(reference?.text || 'not available');
    }

    return summary.addBreak();
}

function addSummaryCommentReferences(summary, postedComments) {
    if (!postedComments?.length) {
        summary.addRaw('Pull request comments: none posted').addBreak();
        return summary;
    }

    summary.addRaw('Pull request comments:').addBreak();

    for (const comment of postedComments) {
        summary.addRaw('- ');

        if (comment.htmlUrl) {
            summary.addLink(`PR #${comment.pullRequestNumber} comment`, comment.htmlUrl);
        }
        else {
            summary.addRaw(`PR #${comment.pullRequestNumber} comment`);
        }

        summary.addBreak();
    }

    return summary;
}

async function writeAnalysisSummary({
    summary,
    failedJobs,
    retryableJobs,
    skippedJobs,
    maxRetryableJobs = defaultMaxRetryableJobs,
    dryRun,
    rerunEligible,
    sourceRunUrl,
    sourceRunAttempt,
    testPatternMatchedTests = [],
}) {
    const analyzedRunReference = buildSummaryReference(
        buildWorkflowRunAttemptUrl(sourceRunUrl, sourceRunAttempt),
        Number.isInteger(sourceRunAttempt) && sourceRunAttempt > 0
            ? `workflow run attempt ${sourceRunAttempt}`
            : 'workflow run'
    );
    const outcome = rerunEligible ? 'Rerun eligible' : 'Rerun skipped';
    const outcomeDetails = rerunEligible
        ? dryRun
            ? `Matched ${retryableJobs.length} retry-safe job${retryableJobs.length === 1 ? '' : 's'} that would be rerun if dry run were disabled.`
            : `Matched ${retryableJobs.length} retry-safe job${retryableJobs.length === 1 ? '' : 's'} for rerun.`
        : retryableJobs.length === 0
            ? 'No retry-safe jobs were found in the analyzed run.'
            : retryableJobs.length > maxRetryableJobs
                ? `Matched ${retryableJobs.length} jobs, which exceeds the cap of ${maxRetryableJobs}.`
                : 'The analyzed run did not satisfy the workflow safety rails for reruns.';
    const summaryRows = [
        [{ data: 'Category', header: true }, { data: 'Count', header: true }],
        ['Outcome', outcome],
        ['Failed jobs inspected', String(failedJobs.length)],
        ['Retryable jobs', String(retryableJobs.length)],
        ['Skipped jobs', String(skippedJobs.length)],
        ['Max retryable jobs', String(maxRetryableJobs)],
        ['Dry run', String(dryRun)],
        ['Eligible to rerun', String(rerunEligible)],
    ];

    await summary
        .addHeading(outcome)
        .addTable(summaryRows);

    addSummaryReference(summary, 'Analyzed run', analyzedRunReference)
        .addRaw(outcomeDetails)
        .addBreak()
        .addBreak();

    if (retryableJobs.length > 0) {
        await summary.addHeading('Retryable jobs', 2);
        await summary.addTable([
            [{ data: 'Job', header: true }, { data: 'Reason', header: true }],
            ...retryableJobs.map(job => [job.name, job.reason]),
        ]);
    }

    if (testPatternMatchedTests.length > 0) {
        await summary.addHeading('Matched test failure patterns', 2);
        const displayedTests = testPatternMatchedTests.slice(0, 25);
        await summary.addTable([
            [{ data: 'Test', header: true }, { data: 'Reason', header: true }],
            ...displayedTests.map(test => [test.testName, test.reason]),
        ]);

        if (testPatternMatchedTests.length > 25) {
            summary.addRaw(`...and ${testPatternMatchedTests.length - 25} more matched test(s).`).addBreak();
        }
    }

    if (skippedJobs.length > 0) {
        await summary.addHeading('Skipped jobs', 2);
        await summary.addTable([
            [{ data: 'Job', header: true }, { data: 'Reason', header: true }],
            ...skippedJobs.slice(0, 25).map(job => [job.name, job.reason]),
        ]);
    }

    await summary.write();
}

// FORCE_RERUN_ALL summary. Force mode does not enumerate or classify jobs, so the
// analyze-step summary is intentionally minimal: it records the eligibility decision
// (the attempt cap is the only force-mode eligibility rule) and the analyzed run, with
// no job/skip tables.
async function writeForceRerunSummary({
    summary,
    rerunEligible,
    dryRun,
    sourceRunUrl,
    sourceRunAttempt,
    runAttempt,
    maxRunAttempt = defaultMaxRunAttempt,
    openPullRequestNumbers = [],
}) {
    const analyzedRunReference = buildSummaryReference(
        buildWorkflowRunAttemptUrl(sourceRunUrl, sourceRunAttempt),
        Number.isInteger(sourceRunAttempt) && sourceRunAttempt > 0
            ? `workflow run attempt ${sourceRunAttempt}`
            : 'workflow run'
    );
    const outcome = rerunEligible ? 'Rerun eligible' : 'Rerun skipped';
    // In force mode the only way to be ineligible is the attempt cap: this summary runs
    // after the open-PR gate (the zero-PR case returns earlier), and force-mode
    // computeRerunEligibility returns false solely when runAttempt > maxRunAttempt. The
    // The skipped case is reachable whenever an automatic or manual run is past the cap.
    const outcomeDetails = rerunEligible
        ? dryRun
            ? 'Force-rerun mode: the failed jobs would be rerun if dry run were disabled (transient-failure analysis bypassed).'
            : 'Force-rerun mode: re-running the failed jobs (transient-failure analysis bypassed).'
        : `Force-rerun mode: the attempt cap was reached (attempt ${runAttempt} > ${maxRunAttempt}); no rerun was requested.`;

    const summaryRows = [
        [{ data: 'Category', header: true }, { data: 'Value', header: true }],
        ['Mode', 'Force rerun (transient-failure analysis bypassed)'],
        ['Outcome', outcome],
        ['Source run attempt', String(Number.isInteger(runAttempt) ? runAttempt : sourceRunAttempt ?? 'unknown')],
        ['Max run attempt', String(maxRunAttempt)],
        ['Associated pull requests', String(openPullRequestNumbers.length)],
        ['Dry run', String(dryRun)],
        ['Eligible to rerun', String(rerunEligible)],
    ];

    await summary
        .addHeading(outcome)
        .addTable(summaryRows);

    addSummaryReference(summary, 'Analyzed run', analyzedRunReference)
        .addRaw(outcomeDetails)
        .addBreak()
        .addBreak();

    await summary.write();
}

async function getOpenPullRequestNumbers({ github, owner, repo, pullRequestNumbers }) {
    const openPullRequestNumbers = [];

    for (const rawPullRequestNumber of new Set(pullRequestNumbers || [])) {
        const pullRequestNumber = Number(rawPullRequestNumber);

        if (!Number.isInteger(pullRequestNumber) || pullRequestNumber <= 0) {
            continue;
        }

        const response = await github.request('GET /repos/{owner}/{repo}/issues/{issue_number}', {
            owner,
            repo,
            issue_number: pullRequestNumber,
        });

        if (response.data.state === 'open' && response.data.pull_request) {
            openPullRequestNumbers.push(pullRequestNumber);
        }
    }

    return openPullRequestNumbers;
}

function buildWorkflowRunAttemptUrl(sourceRunUrl, runAttempt) {
    if (!sourceRunUrl || !Number.isInteger(runAttempt) || runAttempt <= 0) {
        return sourceRunUrl;
    }

    return `${sourceRunUrl.replace(/\/$/, '')}/attempts/${runAttempt}`;
}

function buildWorkflowRunReference(sourceRunUrl, runAttempt) {
    return buildSummaryReference(
        buildWorkflowRunAttemptUrl(sourceRunUrl, runAttempt),
        Number.isInteger(runAttempt) && runAttempt > 0
            ? `workflow run attempt ${runAttempt}`
            : 'workflow run'
    );
}

function formatMarkdownLink(text, url) {
    return url ? `[${text}](${url})` : text;
}

async function getLatestRunAttempt({ github, owner, repo, runId }) {
    if (!Number.isInteger(runId) || runId <= 0) {
        return null;
    }

    try {
        const response = await github.request('GET /repos/{owner}/{repo}/actions/runs/{run_id}', {
            owner,
            repo,
            run_id: runId,
        });

        const runAttempt = Number(response.data.run_attempt);
        return Number.isInteger(runAttempt) && runAttempt > 0 ? runAttempt : null;
    }
    catch {
        return null;
    }
}

function sanitizeMarkdown(text) {
    // Escape backticks and pipe characters to prevent markdown injection
    return String(text).replace(/[`|]/g, ch => `\\${ch}`);
}

function buildPullRequestCommentBody({
    failedAttemptUrl,
    rerunAttemptUrl,
    retryableJobs,
    testPatternMatchedTests = [],
    forceRerunAll = false,
}) {
    // FORCE_RERUN_ALL: force mode is a plain short-circuit — no jobs are fetched or
    // classified, so there is no job list to show. Keep the comment to a single short
    // line: the failed jobs are being retried. See the file-level comment.
    if (forceRerunAll) {
        return `Retrying the failed CI jobs for this pull request from ${formatMarkdownLink('the CI run attempt', failedAttemptUrl)}. The rerun is being tracked in ${formatMarkdownLink('the rerun attempt', rerunAttemptUrl)}.`;
    }

    const lines = [
        `Re-running the failed jobs in the CI workflow for this pull request because ${retryableJobs.length} job${retryableJobs.length === 1 ? ' was' : 's were'} identified as retry-safe transient failures in ${formatMarkdownLink('the CI run attempt', failedAttemptUrl)}.`,
        `GitHub was asked to rerun all failed jobs for that attempt, and the rerun is being tracked in ${formatMarkdownLink('the rerun attempt', rerunAttemptUrl)}.`,
        'The job links below point to the failed attempt jobs that matched the retry-safe transient failure rules.',
        '',
        ...retryableJobs.map(job => {
            const jobReference = job.htmlUrl
                ? `[${job.name}](${job.htmlUrl})`
                : `\`${job.name}\``;

            return `- ${jobReference} - ${job.reason}`;
        }),
    ];

    if (testPatternMatchedTests.length > 0) {
        const displayedTests = testPatternMatchedTests.slice(0, 10);
        lines.push(
            '',
            `<details><summary>Matched test failure patterns (${testPatternMatchedTests.length} test${testPatternMatchedTests.length === 1 ? '' : 's'})</summary>`,
            '',
            ...displayedTests.map(test => `- \`${sanitizeMarkdown(test.testName)}\` — ${test.reason}`),
        );

        if (testPatternMatchedTests.length > 10) {
            lines.push(`- ...and ${testPatternMatchedTests.length - 10} more`);
        }

        lines.push('', '</details>');
    }

    return lines.join('\n');
}

async function addPullRequestComments({ github, owner, repo, pullRequestNumbers, body }) {
    const postedComments = [];

    for (const pullRequestNumber of pullRequestNumbers) {
        const response = await github.request('POST /repos/{owner}/{repo}/issues/{issue_number}/comments', {
            owner,
            repo,
            issue_number: pullRequestNumber,
            body,
        });

        postedComments.push({
            pullRequestNumber,
            htmlUrl: response.data?.html_url || null,
        });
    }

    return postedComments;
}

async function writeRerunOutcomeSummary({
    summary,
    sourceRunUrl,
    sourceRunAttempt,
    heading = 'Rerun skipped',
    message,
}) {
    const failedAttemptReference = buildWorkflowRunReference(sourceRunUrl, sourceRunAttempt);
    await summary.addHeading(heading);
    addSummaryReference(summary, 'Analyzed run', failedAttemptReference)
        .addRaw(message)
        .addBreak()
        .addBreak();
    await summary.write();
}

async function requestFailedJobsRerun({
    github,
    owner,
    repo,
    retryableJobs,
    pullRequestNumbers = [],
    summary,
    sourceRunId,
    sourceRunUrl,
    sourceRunAttempt,
    mainRerunState,
    testPatternMatchedTests = [],
    forceRerunAll = false,
}) {
    // Normal mode lists retry-safe jobs first; an empty list means nothing to rerun.
    // Force mode is a short-circuit that does not enumerate jobs (retryableJobs is
    // empty by design), so it must proceed to the rerun regardless.
    if (!forceRerunAll && retryableJobs.length === 0) {
        return;
    }

    const openPullRequestNumbers = await getOpenPullRequestNumbers({
        github,
        owner,
        repo,
        pullRequestNumbers,
    });

    // The open-PR gate applies in all modes (including force mode): a rerun is
    // skipped when every associated PR is closed. There is no value in spending CI on
    // a closed/merged PR, so force mode does NOT bypass this.
    if (pullRequestNumbers.length > 0 && openPullRequestNumbers.length === 0) {
        const failedAttemptReference = buildWorkflowRunReference(sourceRunUrl, sourceRunAttempt);
        await summary
            .addHeading('Rerun skipped');

        addSummaryReference(summary, 'Analyzed run', failedAttemptReference)
            .addRaw('All associated pull requests are closed. No jobs were rerun.')
            .addBreak()
            .addBreak();

        await summary
            .addHeading('Retryable jobs', 2)
            .addTable([
                [{ data: 'Job', header: true }, { data: 'Reason', header: true }],
                ...retryableJobs.map(job => [job.name, job.reason]),
            ])
            .write();
        return;
    }


    try {
        await github.request('POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs', {
            owner,
            repo,
            run_id: sourceRunId,
        });
    }
    catch (error) {
        if (!mainRerunState) {
            throw error;
        }

        await writeRerunOutcomeSummary({
            summary,
            sourceRunUrl,
            sourceRunAttempt,
            heading: 'Rerun request failed',
            message: 'The failed-job rerun request did not complete successfully. GitHub may still have started a rerun.',
        });
        return {
            ...mainRerunState,
            outcome: 'failed',
            reason: 'request-failed',
        };
    }

    const normalizedSourceRunAttempt = Number.isInteger(sourceRunAttempt) && sourceRunAttempt > 0
        ? sourceRunAttempt
        : null;
    const failedAttemptUrl = buildWorkflowRunAttemptUrl(sourceRunUrl, normalizedSourceRunAttempt);
    const latestRunAttempt = mainRerunState
        ? null
        : await getLatestRunAttempt({
            github,
            owner,
            repo,
            runId: sourceRunId,
        });
    const rerunAttemptNumber = latestRunAttempt && normalizedSourceRunAttempt && latestRunAttempt > normalizedSourceRunAttempt
        ? latestRunAttempt
        : normalizedSourceRunAttempt ? normalizedSourceRunAttempt + 1 : null;
    const rerunAttemptUrl = buildWorkflowRunAttemptUrl(sourceRunUrl, rerunAttemptNumber);
    const failedAttemptReference = buildWorkflowRunReference(sourceRunUrl, normalizedSourceRunAttempt);
    const rerunAttemptReference = buildWorkflowRunReference(sourceRunUrl, rerunAttemptNumber);
    let postedComments = [];

    if (openPullRequestNumbers.length > 0) {
        postedComments = await addPullRequestComments({
            github,
            owner,
            repo,
            pullRequestNumbers: openPullRequestNumbers,
            body: buildPullRequestCommentBody({
                failedAttemptUrl: failedAttemptReference.url,
                rerunAttemptUrl: rerunAttemptReference.url,
                retryableJobs,
                testPatternMatchedTests,
                forceRerunAll,
            }),
        });
    }

    const summaryBuilder = summary
        .addHeading('Rerun requested');

    addSummaryReference(summaryBuilder, 'Failed attempt', failedAttemptReference);
    addSummaryReference(summaryBuilder, 'Rerun attempt', rerunAttemptReference);
    addSummaryCommentReferences(summaryBuilder, postedComments);

    // Force mode does not enumerate jobs, so there is no retry-safe job table to show.
    if (forceRerunAll) {
        summaryBuilder
            .addBreak()
            .addRaw('Force-rerun mode: the CI run failed, so GitHub was asked to rerun all failed jobs for the failed attempt. Transient-failure analysis was bypassed.')
            .addBreak();

        await summaryBuilder.write();
        return mainRerunState;
    }

    summaryBuilder
        .addBreak()
        .addRaw('The matched jobs below made the run eligible for rerun. GitHub was asked to rerun all failed jobs for the failed attempt.')
        .addBreak()
        .addBreak()
        .addHeading('Retryable jobs', 2);

    summaryBuilder
        .addTable([
            [{ data: 'Job', header: true }, { data: 'Reason', header: true }],
            ...retryableJobs.map(job => [job.name, job.reason]),
        ]);

    await summaryBuilder.write();
    return mainRerunState;
}

function formatMatchedPatternForMarkdown(matchedPattern) {
    if (!matchedPattern) {
        return '';
    }

    const patternText = String(matchedPattern);
    let maxBacktickRun = 0;
    const backtickRunRegex = /`+/g;
    let match;

    while ((match = backtickRunRegex.exec(patternText)) !== null) {
        if (match[0].length > maxBacktickRun) {
            maxBacktickRun = match[0].length;
        }
    }

    const fence = '`'.repeat(maxBacktickRun + 1);
    return ` Matched pattern: ${fence}${patternText}${fence}.`;
}


module.exports = {
    failureConclusions,
    ignoredJobs,
    defaultMaxRetryableJobs,
    defaultMaxRunAttempt,
    defaultDelay,
    postTestCleanupFailureStepPatterns,
    windowsProcessInitializationFailurePatterns,
    matchesAny,
    findMatchingPattern,
    createBoundedDiagnosticPairPatterns,
    parseCheckRunId,
    getPullRequestNumbers,
    getHeadRepositoryOwnerLogin,
    matchesWorkflowRunHead,
    listPullRequestsByHead,
    getAssociatedPullRequestNumbers,
    getCheckRunIdForJob,
    getFailedSteps,
    annotationText,
    toAnnotationText,
    computeRerunEligibility,
    computeRerunExecutionEligibility,
    buildSummaryReference,
    addSummaryReference,
    addSummaryCommentReferences,
    writeAnalysisSummary,
    writeForceRerunSummary,
    getOpenPullRequestNumbers,
    buildWorkflowRunAttemptUrl,
    buildWorkflowRunReference,
    formatMarkdownLink,
    getLatestRunAttempt,
    sanitizeMarkdown,
    buildPullRequestCommentBody,
    addPullRequestComments,
    writeRerunOutcomeSummary,
    requestFailedJobsRerun,
    formatMatchedPatternForMarkdown,
};
