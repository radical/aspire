module.exports = async ({ github, context, core }) => {
    const { data: pr } = await github.rest.pulls.get({
        owner: context.repo.owner,
        repo: context.repo.repo,
        pull_number: context.issue.number
    });

    const baseRepository = `${context.repo.owner}/${context.repo.repo}`;
    if (pr.head.repo?.full_name !== baseRepository) {
        throw new Error(`Deployment tests can only run for branches in ${baseRepository}; fork PRs are not supported.`);
    }

    core.setOutput('number', pr.number);
    core.setOutput('head_sha', pr.head.sha);
    core.setOutput('head_ref', pr.head.ref);
};
