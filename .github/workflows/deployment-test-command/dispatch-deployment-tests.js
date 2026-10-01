module.exports = async ({ github, context, core }) => {
    const headRef = process.env.PR_HEAD_REF;
    const prNumber = process.env.PR_NUMBER;

    if (!headRef) {
        throw new Error('PR head ref was not provided.');
    }

    if (!prNumber || !/^\d+$/.test(prNumber)) {
        throw new Error('PR number was not provided or is invalid.');
    }

    // PR-controlled values remain data rather than being interpolated into JavaScript source.
    await github.rest.actions.createWorkflowDispatch({
        owner: context.repo.owner,
        repo: context.repo.repo,
        workflow_id: 'deployment-tests.yml',
        ref: headRef,
        inputs: {
            pr_number: prNumber
        }
    });

    core.info('Triggered deployment-tests.yml workflow');
};
