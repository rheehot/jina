""" Script to change versioning of files (eg. manifest.yml) for executors
[encoders, crafters, indexers, rankers, evaluators, classifiers etc.].
It also adds the required jina version.
Commits the change in the branch and raises a PR for the executor.
Then attempts to automatically merge the PRs
"""
import glob
import os
import sys
import time
import traceback
from typing import List, Optional, Tuple

import git
import requests
import semver
from github import Github, PullRequestMergeStatus, Repository
from github.Issue import Issue
from github.PullRequest import PullRequest
from ruamel.yaml import YAML

WAIT_BETWEEN_PR_CHECKS = 5 * 60
FIX_MODULE_TEMPLATE = 'fix module '
TAG_IN_ISSUES = os.environ.get('TAG_IN_ISSUES', '')
MODULES_REPO = os.environ.get('MODULES_REPO')
if MODULES_REPO is None:
    print(f'Error: MODULES_REPO needs to be set. Exiting...')
    sys.exit(1)

# this one has PR push access
g = Github(os.environ["GITHUB_TOKEN"])

yaml = YAML()


def create_pr(fpath, module, jina_core_version, hub_repo, hub_origin, gh_hub_repo) -> Optional[PullRequest]:
    """for each module with manifest.yml attempts to bump version and add jina core version.
    then opens PR"""
    pr = None
    print(f'handling {module}...')
    with open(fpath) as fp:
        info = yaml.load(fp)
        # make sure the (possibly) existing version is older
        if 'jina_version' in info.keys():
            # TODO should this be list?
            existing_jina_version = info['jina_version']
            if semver.VersionInfo.parse(existing_jina_version) >= semver.VersionInfo.parse(jina_core_version):
                print(f'existing jina-core version for {module} was greater or equal than version to update '
                      f'({existing_jina_version} >= {jina_core_version}). Skipping...')
                return

        old_ver = info['version']
        new_ver = '.'.join(old_ver.split('.')[:-1] + [str(int(old_ver.split('.')[-1]) + 1)])
        info['version'] = new_ver
        print(f'bumped to {new_ver}')
        info['jina_version'] = jina_core_version
    with open(fpath, 'w') as fp:
        yaml.dump(info, fp)

    br_name = ''
    try:
        print('preparing the branch ...')
        br_name = f'chore-{module.lower()}-{new_ver.replace(".", "-")}-core-{jina_core_version.replace(".", "-")}'
        new_branch = hub_repo.create_head(br_name)
        new_branch.checkout()

        print(f'bumping version to {new_ver} and committing to {new_branch}...')
        hub_repo.git.add(update=True)
        # NOTE limited to 72 characters by commit lint
        hub_repo.index.commit(f'chore: bump {module} version to {new_ver}')
        hub_repo.git.push('--set-upstream', hub_origin, hub_repo.head.ref)

        print('making a PR ...')
        title_string = f'bumping version for {module} to {new_ver} (new jina: {jina_core_version})'
        body_string = f'Due to the release of jina core v{jina_core_version}, this PR is automatically submitted to ' \
                      f'ensure compatibility '

        pr = gh_hub_repo.create_pull(
            title=title_string,
            body=body_string,
            head=br_name,
            base='master'
        )
    except git.GitCommandError as e:
        print(f'Caught exception: {repr(e)}')
        if 'tip of your current branch is behind' in str(e) \
                or 'the remote contains work that you do' in str(e):
            print(f'warning: Branch "{br_name}" already existed. Attempting to get pr from GH...')
            prs = list(gh_hub_repo.get_pulls(
                head=f'{MODULES_REPO}:{br_name}',
                state='open'
            ))
            if len(prs) > 1:
                print(f'Error: Too many PRs matched branch {br_name}: {[p.html_url for p in prs]}')
                return None
            elif len(prs) == 1:
                pr = prs[0]
                print(f'Found existing PR for {br_name}: {pr.html_url}')
                return pr
            else:
                print(f'Couldn\'t retrieve PR for branch. Skipping...')
                return None
    except Exception:
        raise
    finally:
        hub_repo.git.checkout('master')
        if br_name:
            hub_repo.delete_head(br_name, force=True)

    return pr


def all_checks_passed(runs) -> Optional[bool]:
    """
    check whether all checks from a PR head ref have completed and passed
    """
    for c in runs:
        if c['status'] == 'completed':
            if c['conclusion'] == 'failure':
                return False
        else:
            return None
    return True


def comment_fail(pr, br_name):
    pr.create_issue_comment(
        f"Automatic merge failed. Please investigate {TAG_IN_ISSUES}"
    )
    print(f'Merge of {br_name} failed. Check {pr.html_url}')
    sys.exit(1)


def get_checks_for_pr(sha) -> List[Tuple]:
    result = requests.get(
        f'https://api.github.com/repos/{MODULES_REPO}/commits/{sha}/check-runs',
        headers={'Accept': 'application/vnd.github.v3+json'}
    )
    checks = result.json()
    pr_checks = checks['check_runs']
    print(f'Got {len(pr_checks)} runs to check for PR: \n{[(r["name"], r["status"], r["conclusion"]) for r in pr_checks]}')
    return pr_checks


def open_issue(pr, pr_checks, hub_repo: Repository, module):
    """opens an issue for the PR with the failed checks (if not already open)"""
    issue_name = f'{FIX_MODULE_TEMPLATE}{module}'
    existing_issue_for_pr = [
        i for i in list(hub_repo.get_issues(state='open'))
        if i.title == issue_name
    ]
    if len(existing_issue_for_pr) > 0:
        print(f'Found existing issue: {existing_issue_for_pr}')
    else:
        # open the issue
        body = f"""
**[This is an automated issue opened as part of the hub modules update GH action. DO NOT EDIT THIS DESCRIPTION]**

Could not update module **{module}** because some of the checks failed:

```
{[(c['name'], c['status'], c['conclusion']) for c in pr_checks]}
```

See {pr.html_url} for more info. {TAG_IN_ISSUES}
"""
        issue = hub_repo.create_issue(
            title=issue_name,
            body=body,
        )
        print(f'opened issue at {issue.html_url}')
    return


def handle_prs(prs_modules: List[Tuple[PullRequest, str]], hub_repo):
    """
    traverses list of open PRs. Confirms whether checks have passed or not. If they have, merges. If not,
    either tries again or, if they have failed, removes them and comments on the PR :param prs: :return: None when done
    """
    # noinspection PyBroadException
    try:
        # allow for checks to be initiated. It's not instantaneous
        print(f'waiting for 30 secs. before continuing...')
        time.sleep(30)
        new_prs = []
        while len(prs_modules) > 0:
            for i, pr_module in enumerate(prs_modules):
                pr = pr_module[0]
                module = pr_module[1]
                print(f'Checking PR {pr} ( {pr.html_url} )...')
                br_name = pr.head.ref
                last_commit = sorted(list(pr.get_commits()), key=lambda t: t.commit.author.date)[-1]
                sha = last_commit.sha
                pr_checks = get_checks_for_pr(sha)
                checks_passed = all_checks_passed(pr_checks)
                if checks_passed is None:
                    print(f'Not all checks have completed for {br_name}. Skipping and will attempt later...')
                    new_prs.append(pr)
                else:
                    if checks_passed:
                        print(f'All checks completed and passed for {br_name}. Attempting to merge...')
                        # this should work with the DEV BOT TOKEN (as it has root access to all)
                        try:
                            status: PullRequestMergeStatus = pr.merge('automatic merge')
                            print(f'status after merge: {status}')
                        except Exception as e:
                            print(repr(e))
                            comment_fail(pr, br_name)
                    else:
                        print(f'warning: not all checks have passed for {br_name}. Will open issue and abandon trying.')
                        open_issue(pr, pr_checks, hub_repo, module)

            # starting the checking process again on the subset
            # of PRs that had not yet completed
            prs_modules = new_prs
            print(f'Have {len(prs_modules)} PRs left to check')
            if len(prs_modules) > 0:
                print(f'waiting for {WAIT_BETWEEN_PR_CHECKS // 60} mins. before continuing...')
                time.sleep(WAIT_BETWEEN_PR_CHECKS)
            new_prs = []
        print('Done!')
        return
    except Exception:
        print(f'Error occurred: {traceback.format_exc()}')
    return


def get_issues_for_modules(hub_repo: Repository):
    issues: List[Issue] = list(hub_repo.get_issues(
        state='open'
    ))
    issues = [
        i for i in issues if FIX_MODULE_TEMPLATE in i.title
    ]
    names = [
        i.title.split(FIX_MODULE_TEMPLATE)[-1]
        for i in issues
    ]
    print(f'found {len(names)} modules to be fixed: {names}')
    return names


def get_jina_version() -> str:
    """gets stable version of jina"""
    response = requests.get('https://api.jina.ai/latest').json()
    return response['version']


def main():
    modules_dir = MODULES_REPO.split('/')[-1]
    hub_repo = git.Repo(modules_dir)
    hub_origin = hub_repo.remote(name='origin')
    gh_hub_repo: Repository = g.get_repo(MODULES_REPO)
    print(f'initiated modules repo: {hub_repo} with GitHub repo {gh_hub_repo} (origin: {hub_origin})')

    jina_core_version = get_jina_version()
    print(f'got jina core v: "{jina_core_version}"')

    print(f'cur. dir. is "{os.getcwd()}"')

    modules = glob.glob(f'{MODULES_REPO}/**/manifest.yml', recursive=True)
    print(f'got {len(modules)} modules to update')

    prs: List[Tuple[PullRequest, str]] = []
    # filter those for which there is an open issue
    # template 'fix module modulename' (ex. 'fix module DeepSegmenter')
    to_be_fixed = get_issues_for_modules(gh_hub_repo)

    # traverse list of modules in jina-hub
    for fpath in modules:
        module = fpath.split('/')[-2]
        if module not in to_be_fixed:
            pr = create_pr(fpath, module, jina_core_version, hub_repo, hub_origin, gh_hub_repo)
            if pr:
                prs.append((pr, module))
        else:
            print(f'skipping {module} as there is an open issue for it...')

    handle_prs(prs, gh_hub_repo)


if __name__ == '__main__':
    main()
