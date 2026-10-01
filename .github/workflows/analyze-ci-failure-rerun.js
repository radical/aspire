const fs = require('node:fs');
const path = require('node:path');

async function run({ github, context, core }) {

    // Read inputs from the agent output artifact.
    // gh-aw writes { "items": [ { "type": "rerun_failed_jobs", ... } ] }.
    const outputFile = process.env.GH_AW_AGENT_OUTPUT;
    if (!outputFile || !fs.existsSync(outputFile)) {
      core.setFailed('Agent output file not found');
      return;
    }
    const payload = JSON.parse(fs.readFileSync(outputFile, 'utf8'));
    const items = (payload && Array.isArray(payload.items)) ? payload.items : [];
    const item = items.find(i => i && i.type === 'rerun_failed_jobs');
    if (!item) {
      core.info('No rerun_failed_jobs items in agent output.');
      return;
    }

    // The analysis JSON and cause files ship in the `ci-analysis-output` artifact,
    // which the `download-analysis` step unpacks. They are not siblings of the agent
    // output, so this path must come from that step rather than being derived.
    const analysisDir = process.env.ANALYSIS_DIR;
    if (!analysisDir) {
      core.setFailed('ANALYSIS_DIR is required (download-analysis step missing?)');
      return;
    }
    const analysisFile = path.join(analysisDir, 'analysis-result.json');
    const causesDir = path.join(analysisDir, 'causes');
    const runContextFile = path.join('ci-failure-data', 'run-context.json');
    const trustedFailedJobsFile = path.join('ci-failure-data', 'failed-jobs.json');
    const testEvidenceFile = path.join('ci-failure-data', 'test-evidence.json');
    const trustedTestFailuresFile = path.join('ci-failure-data', 'test-failures.json');
    const priorCausesDir = path.join('ci-failure-data', 'prior-causes');
    if (!fs.existsSync(analysisFile) ||
        !fs.existsSync(runContextFile) ||
        !fs.existsSync(trustedFailedJobsFile) ||
        !fs.existsSync(testEvidenceFile)) {
      core.setFailed('Analysis result or trusted run data not found');
      return;
    }

    const analysis = JSON.parse(fs.readFileSync(analysisFile, 'utf8'));
    const runContext = JSON.parse(fs.readFileSync(runContextFile, 'utf8'));
    const trustedFailedJobs = JSON.parse(fs.readFileSync(trustedFailedJobsFile, 'utf8'));
    const testEvidence = JSON.parse(fs.readFileSync(testEvidenceFile, 'utf8'));
    const owner = context.repo.owner;
    const repo = context.repo.repo;
    const requestedRunId = Number(item.run_id);
    const trustedRunId = Number(runContext.run_id);
    const trustedRunAttempt = Number(runContext.run_attempt);
    const trustedPrNumberText = String(runContext.pr_numbers || '');
    const trustedRunScope = String(runContext.run_scope || '');
    const enableRerun = String(process.env.ENABLE_RERUN).toLowerCase() === 'true';

    if (!Number.isInteger(requestedRunId) || requestedRunId <= 0) {
      core.setFailed(`Invalid run_id: ${item.run_id}`);
      return;
    }
    if (!Number.isInteger(trustedRunId) || trustedRunId <= 0) {
      core.setFailed(`Invalid trusted run_id: ${runContext.run_id}`);
      return;
    }
    if (!Number.isInteger(trustedRunAttempt) || trustedRunAttempt <= 0) {
      core.setFailed(`Invalid trusted run attempt: ${runContext.run_attempt}`);
      return;
    }
    if (requestedRunId !== trustedRunId) {
      core.setFailed('Rerun request does not match trusted run context');
      return;
    }
    if (Number(analysis.run_id) !== trustedRunId ||
        analysis.run_scope !== trustedRunScope ||
        analysis.verdict !== 'transient-infra') {
      core.setFailed('Rerun requires a trusted transient-infra analysis for the same run');
      return;
    }
    if (trustedRunScope !== 'main' && trustedRunScope !== 'pull-request') {
      core.setFailed(`Unsupported trusted run scope: ${trustedRunScope}`);
      return;
    }
    if (!Array.isArray(analysis.failed_jobs) ||
        analysis.failed_jobs.length === 0 ||
        !analysis.failed_jobs.every(job => job && Number.isInteger(job.id)) ||
        !analysis.failed_jobs.every(job => job && job.classification === 'transient-infra')) {
      core.setFailed('Rerun requires every failed job to be classified as transient-infra');
      return;
    }
    if (!Array.isArray(analysis.failed_tests) || analysis.failed_tests.length !== 0) {
      core.setFailed('Rerun requires a transient-infra analysis without failed tests');
      return;
    }
    if (!testEvidence ||
        typeof testEvidence !== 'object' ||
        (testEvidence.state !== 'complete' && testEvidence.state !== 'not-applicable')) {
      core.setFailed('Rerun requires available trusted test evidence');
      return;
    }
    if (testEvidence.state === 'complete') {
      if (!fs.existsSync(trustedTestFailuresFile)) {
        core.setFailed('Rerun requires complete trusted test evidence without failed tests');
        return;
      }

      const trustedTestFailures = JSON.parse(fs.readFileSync(trustedTestFailuresFile, 'utf8'));
      if (!Array.isArray(trustedTestFailures) || trustedTestFailures.length !== 0) {
        core.setFailed('Rerun requires complete trusted test evidence without failed tests');
        return;
      }
    }
    if (!Array.isArray(trustedFailedJobs) ||
        !trustedFailedJobs.every(job => job && Number.isInteger(job.id))) {
      core.setFailed('Trusted failed jobs are invalid');
      return;
    }
    const analysisJobIds = analysis.failed_jobs.map(job => job.id);
    const trustedJobIds = trustedFailedJobs.map(job => job.id);
    const analysisJobIdSet = new Set(analysisJobIds);
    const trustedJobIdSet = new Set(trustedJobIds);
    if (analysisJobIdSet.size !== analysisJobIds.length ||
        analysisJobIdSet.size !== trustedJobIdSet.size ||
        !analysisJobIds.every(jobId => trustedJobIdSet.has(jobId))) {
      core.setFailed('Analysis failed-job IDs do not match the trusted failed jobs');
      return;
    }

    const summaryCauseIds = Array.isArray(analysis.causes) ? analysis.causes : [];
    const causeFiles = fs.existsSync(causesDir)
      ? fs.readdirSync(causesDir).filter(fileName => fileName.endsWith('.json'))
      : [];
    const maxCauseCount = 10;
    if (summaryCauseIds.length > maxCauseCount || causeFiles.length > maxCauseCount) {
      core.setFailed(`Rerun analysis exceeds the ${maxCauseCount}-cause publication budget`);
      return;
    }
    if (summaryCauseIds.length === 0 ||
        !summaryCauseIds.every(causeId => typeof causeId === 'string') ||
        new Set(summaryCauseIds).size !== summaryCauseIds.length ||
        causeFiles.length !== summaryCauseIds.length) {
      core.setFailed('Rerun requires unique analysis cause IDs matching the generated cause files');
      return;
    }
    const causeJobIdCoverage = new Set();
    for (const causeFileName of causeFiles) {
      let cause;
      try {
        cause = JSON.parse(fs.readFileSync(path.join(causesDir, causeFileName), 'utf8'));
      } catch (error) {
        core.setFailed(`Invalid JSON in rerun cause file ${causeFileName}: ${error.message}`);
        return;
      }

      const causeId = String(cause.id || '');
      if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(causeId) ||
          `${causeId}.json` !== causeFileName ||
          cause.type !== 'infra-failure' ||
          !summaryCauseIds.includes(causeId)) {
        core.setFailed(`Rerun cause ${causeFileName} must be a valid infra-failure cause`);
        return;
      }

      if (!Array.isArray(cause.job_ids) ||
          cause.job_ids.length === 0 ||
          !cause.job_ids.every(jobId => Number.isInteger(jobId) && jobId > 0) ||
          new Set(cause.job_ids).size !== cause.job_ids.length ||
          !cause.job_ids.every(jobId => trustedJobIdSet.has(jobId))) {
        core.setFailed(`Rerun cause ${causeFileName} has invalid or untrusted job_ids`);
        return;
      }
      for (const jobId of cause.job_ids) {
        causeJobIdCoverage.add(jobId);
      }

      const priorCauseFile = path.join(priorCausesDir, causeFileName);
      if (fs.existsSync(priorCauseFile)) {
        let priorCause;
        try {
          priorCause = JSON.parse(fs.readFileSync(priorCauseFile, 'utf8'));
        } catch {
          core.setFailed(`Invalid JSON in prior rerun cause file ${causeFileName}`);
          return;
        }
        if (!priorCause || typeof priorCause !== 'object' || typeof priorCause.type !== 'string') {
          core.setFailed(`Prior rerun cause ${causeFileName} must be an object with a string type`);
          return;
        }
        if (priorCause.type !== cause.type) {
          core.setFailed(`Rerun cause ${causeFileName} cannot change stored type from '${priorCause.type}' to '${cause.type}'`);
          return;
        }
      }
    }

    if (!analysisJobIds.every(jobId => causeJobIdCoverage.has(jobId))) {
      core.setFailed('Rerun cause job_ids do not cover every trusted failed job');
      return;
    }

    if (!enableRerun) {
      core.info(`Dry-run mode (ENABLE_RERUN is not 'true'). Would have rerun failed jobs for run ${trustedRunId}.`);
      return;
    }

    if (trustedRunScope === 'pull-request') {
      if (!/^[1-9][0-9]*$/.test(trustedPrNumberText)) {
        core.info('No unambiguous subject PR is available. Skipping rerun.');
        return;
      }

      const trustedPrNumber = Number(trustedPrNumberText);
      try {
        const { data: pr } = await github.rest.pulls.get({ owner, repo, pull_number: trustedPrNumber });
        if (pr.state !== 'open') {
          core.info('The subject PR is closed. Skipping rerun.');
          return;
        }
        if (pr.locked) {
          core.info('The subject PR is locked. Skipping rerun.');
          return;
        }
      } catch (e) {
        core.warning(`Failed to check PR #${trustedPrNumber}: ${e.message}`);
        return;
      }
    }

    const { data: currentRun } = await github.rest.actions.getWorkflowRun({
      owner,
      repo,
      run_id: trustedRunId,
    });
    if (currentRun.run_attempt !== trustedRunAttempt) {
      core.warning(`Run ${trustedRunId} advanced from attempt ${trustedRunAttempt} to ${currentRun.run_attempt}. Skipping stale rerun request.`);
      return;
    }

    // Request rerun of failed jobs
    await github.rest.actions.reRunWorkflowFailedJobs({
      owner,
      repo,
      run_id: trustedRunId,
    });

    core.info(`Requested rerun of failed jobs for run ${trustedRunId}.`);
}

module.exports = { run };
