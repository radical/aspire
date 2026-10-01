// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const run = require(process.argv[2]);
const command = process.argv[3];
const scenario = process.argv[4];

if (command === 'permission') {
    await runPermissionScenario();
} else if (command === 'pull-request') {
    await runPullRequestScenario();
} else if (command === 'dispatch') {
    await runDispatchScenario();
} else {
    throw new Error(`Unknown command: ${command}`);
}

async function runPermissionScenario() {
    const body = process.argv[5];
    const validCommand = process.argv[6] === 'true';
    const allowed = ['write', 'admin', 'maintain'].includes(scenario);
    const apiError = scenario.startsWith('error-')
        ? Object.assign(new Error('Permission lookup failed'), { status: Number(scenario.slice(6)) })
        : undefined;
    const outputs = {};
    const failures = [];
    const comments = [];
    let lookups = 0;
    const context = {
        repo: { owner: 'microsoft', repo: 'aspire' },
        issue: { number: 42 },
        payload: { comment: { body, user: { login: 'contributor' } } },
        actor: 'different-user'
    };
    const github = {
        rest: {
            repos: {
                getCollaboratorPermissionLevel: async ({ owner, repo, username }) => {
                    lookups++;
                    assert.equal(owner, 'microsoft');
                    assert.equal(repo, 'aspire');
                    assert.equal(username, 'contributor');
                    if (apiError) {
                        throw apiError;
                    }

                    // GitHub normalizes the maintain role to permission: "write".
                    // https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user
                    return { data: { permission: scenario === 'maintain' ? 'write' : scenario, role_name: scenario } };
                }
            },
            issues: {
                createComment: async ({ owner, repo, issue_number, body }) => {
                    assert.equal(owner, 'microsoft');
                    assert.equal(repo, 'aspire');
                    assert.equal(issue_number, 42);
                    comments.push(body);
                }
            }
        }
    };
    const core = {
        info: () => {},
        setOutput: (name, value) => { outputs[name] = value; },
        setFailed: message => { failures.push(message); }
    };

    if (!validCommand) {
        await run({ github, context, core });
        assert.deepEqual(outputs, { has_write_access: 'false' });
        assert.deepEqual(failures, []);
        assert.deepEqual(comments, []);
    } else if (apiError) {
        await assert.rejects(() => run({ github, context, core }), error => error === apiError);
        assert.deepEqual(outputs, {});
        assert.deepEqual(failures, []);
        assert.deepEqual(comments, []);
    } else {
        await run({ github, context, core });
        assert.deepEqual(outputs, { has_write_access: String(allowed) });
        assert.deepEqual(failures, allowed ? [] : ['@contributor does not have write access to this repository.']);
        assert.deepEqual(comments, allowed ? [] : [
            '@contributor The `/deployment-test` command requires write access to this repository for security reasons (it deploys to real Azure infrastructure).'
        ]);
    }
    assert.equal(lookups, validCommand ? 1 : 0);
}

async function runPullRequestScenario() {
    const outputs = {};
    const context = {
        repo: { owner: 'microsoft', repo: 'aspire' },
        issue: { number: 42 }
    };
    const github = {
        rest: {
            pulls: {
                get: async args => {
                    assert.deepEqual(args, {
                        owner: 'microsoft',
                        repo: 'aspire',
                        pull_number: 42
                    });
                    return {
                        data: {
                            number: 42,
                            head: {
                                sha: 'a'.repeat(40),
                                ref: 'feature',
                                repo: {
                                    full_name: scenario === 'fork' ? 'external/aspire' : 'microsoft/aspire'
                                }
                            }
                        }
                    };
                }
            }
        }
    };
    const core = { setOutput: (name, value) => { outputs[name] = value; } };

    if (scenario === 'fork') {
        await assert.rejects(
            () => run({ github, context, core }),
            /Deployment tests can only run for branches in microsoft\/aspire/);
        assert.deepEqual(outputs, {});
        return;
    }

    await run({ github, context, core });
    assert.deepEqual(outputs, {
        number: 42,
        head_sha: 'a'.repeat(40),
        head_ref: 'feature'
    });
}

async function runDispatchScenario() {
    process.env.PR_HEAD_REF = scenario === 'missing-head' ? '' : 'feature';
    process.env.PR_NUMBER = scenario === 'invalid-number' ? '42-untrusted' : '42';

    const calls = [];
    const apiError = new Error('Dispatch failed');
    const github = {
        rest: {
            actions: {
                createWorkflowDispatch: async args => {
                    calls.push(args);
                    if (scenario === 'api-error') {
                        throw apiError;
                    }
                }
            }
        }
    };
    const context = { repo: { owner: 'microsoft', repo: 'aspire' } };
    const core = { info: () => {} };

    if (scenario === 'missing-head') {
        await assert.rejects(() => run({ github, context, core }), /PR head ref was not provided/);
    } else if (scenario === 'invalid-number') {
        await assert.rejects(() => run({ github, context, core }), /PR number was not provided or is invalid/);
    } else if (scenario === 'api-error') {
        await assert.rejects(() => run({ github, context, core }), error => error === apiError);
    } else {
        await run({ github, context, core });
    }

    assert.deepEqual(calls, scenario === 'missing-head' || scenario === 'invalid-number'
        ? []
        : [{
            owner: 'microsoft',
            repo: 'aspire',
            workflow_id: 'deployment-tests.yml',
            ref: 'feature',
            inputs: { pr_number: '42' }
        }]);
}
