module.exports = async ({ github, context, core }) => {
    // Accept "/deployment-test" or "/deployment-test\r\n..." (also spaces/tabs),
    // but not longer tokens such as "/deployment-testing" or "/deployment-test-disabled".
    // Match case-insensitively like the GitHub Actions startsWith prefilter.
    if (!/^\/deployment-test(?:\s|$)/i.test(context.payload.comment.body)) {
        core.info('Ignoring comment without a /deployment-test command token.');
        core.setOutput('has_write_access', 'false');
        return;
    }

    const commenter = context.payload.comment.user.login;
    const { data: permission } = await github.rest.repos.getCollaboratorPermissionLevel({
        owner: context.repo.owner,
        repo: context.repo.repo,
        username: commenter
    });

    if (!['admin', 'write'].includes(permission.permission)) {
        core.setOutput('has_write_access', 'false');
        core.setFailed(`@${commenter} does not have write access to this repository.`);
        await github.rest.issues.createComment({
            owner: context.repo.owner,
            repo: context.repo.repo,
            issue_number: context.issue.number,
            body: `@${commenter} The \`/deployment-test\` command requires write access to this repository for security reasons (it deploys to real Azure infrastructure).`
        });
        return;
    }

    core.info(`Verified ${commenter} has ${permission.permission} access to the repository.`);
    core.setOutput('has_write_access', 'true');
};
