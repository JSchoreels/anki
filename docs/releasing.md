<!-- DO NOT MANUALLY EDIT THIS FILE -->
<!-- This file is copied from docs-site/developers/releasing.mdx automatically -->

# Releasing

<!-- <<<cog
from cogdocs import get_file_contents
cog.out(get_file_contents("releasing"))
>>> -->

Releases are managed by two GitHub Actions workflows in this fork:

1. **`prepare-release.yml`** — Validates a version, checks CI and duplicate
   tags/releases, commits `.version`, and pushes the dispatching branch.

2. **`release.yml`** — Builds installers and wheels for all platforms (Linux
   x86/ARM, macOS Intel/ARM, Windows), and can optionally sign macOS/Windows
   artifacts, create a draft GitHub release, publish wheels to TestPyPI, and
   publish wheels to PyPI.

Both workflows are manually dispatched and share a `release` concurrency group,
so only one preparation/build operation runs at a time.

## Version format

Versions follow calendar versioning with PEP 440: `YY.MM` for stable releases
(e.g. `26.04`), with optional `.patch` (e.g. `26.04.1`) and pre-release
suffixes (`b1`, `rc1`, `a1`). Months must be zero-padded.

Examples: `26.05b1` (beta), `26.05rc1` (release candidate), `26.05` (stable),
`26.05.1` (patch).

Fork application versions add the local suffix `+fsrs7`, for example
`26.09b1+fsrs7`. Unsigned draft releases append
`.build.<github-actions-run-number>` to the release tag, while the application
keeps the base fork version. The Actions run number is the monotonically
increasing fork build number: never reset, reuse, or manually replace it.

## Synchronizing an upstream Anki release

Use this process whenever this fork is aligned to a new official Anki release:

1. Confirm every developer's work is committed and pushed. Fetch `origin`,
   `upstream`, all collaborator remotes, and tags. Audit unmerged remote commits
   before changing the release branch.
2. Identify the official release tag and review its range from the previous
   upstream tag. Merge that exact tag, not `upstream/main`, so post-release
   commits are excluded:

    ```
    git merge --no-ff <official-tag>
    ```

3. Resolve conflicts by retaining the fork's user-facing behavior and accepting
   the upstream changes included in that tag. Do not merge stale collaborator
   branches whose commits are already present or superseded.
4. Set `.version` to `<official-tag>+fsrs7`; do not put the build number in this
   file. Verify the update endpoints in `rslib/src/backend/github.rs` still use
   `JSchoreels/anki`, and that both automatic and manual update checks use the
   fork's GitHub releases.
5. Update `RELEASE.md`. Link the official upstream release and summarize both
   the upstream alignment and the fork-specific changes retained in the build.
6. Run targeted tests and `just check`. Commit the merge, push the exact release
   commit, and wait for its complete CI matrix to succeed.
7. Dispatch `just release::draft --ref <branch>`. Its preflight waits for CI;
   do not use `--skip-ci-check=true` unless a maintainer explicitly accepts the
   risk. The workflow creates an unsigned draft tag named
   `<version>.build.<run-number>`.
8. Verify the workflow, installers, draft target commit, and release assets.
   Replace generated notes with notes that link the official release and explain
   the fork changes.
9. Publish that verified draft as the current release, preserving its beta/RC
   status when applicable, and verify the GitHub Releases API returns it to the
   application's update checker.

If the draft build fails, fix the release commit, push it, wait for CI again,
and dispatch a new draft. Do not publish the failed draft or reuse its build
number.

## Release branch workflow

All releases are cut from a `release/YY.MM` branch. The branch name uses only
the major version (`YY.MM`), not the full pre-release suffix — betas, release
candidates, and the stable release all come from the same branch.

### Standard release

1. Create a release branch from `main`:

    ```
    git checkout -b release/26.05 main
    git push origin release/26.05
    ```

2. CI runs automatically on push to `release/**` branches.

3. Prepare the release (updates `.version` on the branch):

    ```
    just release::prepare --version 26.05b1 --ref release/26.05
    ```

4. Pull the preparation commit, then verify on TestPyPI:

    ```
    git pull origin release/26.05
    just release::testpypi --ref release/26.05
    ```

5. Publish the full release:

    ```
    just release::public --ref release/26.05
    ```

6. For subsequent pre-releases or the stable release from the same cycle,
   repeat steps 3-5 with the new version (e.g. `26.05b2`, `26.05rc1`, `26.05`).

7. After the stable release, merge the release branch back to `main` to pick up
   the `.version` bump and any cherry-picked fixes:

    ```
    git checkout main
    git merge release/26.05
    git push origin main
    ```

8. Delete the release branch after the stable release is published.

### Security and hotfix releases

For security fixes, an admin should first create a
[security advisory](https://github.com/ankitects/anki/security/advisories)
with a temporary private fork. Work on the fix in the private fork via the
normal PR workflow. Do not open a public PR or publish the advisory until
the fix is ready for release.

Once the fix is ready:

1. Create a release branch from the latest release tag:

    ```
    git checkout -b release/26.05 26.05
    ```

2. Cherry-pick the fix onto the release branch.

3. Push the branch and wait for CI:

    ```
    git push origin release/26.05
    ```

4. Prepare and publish:

    ```
    just release::prepare --version 26.05.1
    just release::public --ref release/26.05
    ```

5. Merge the release branch back to `main`.

6. For security patches, publish the advisory and credit the reporter if
   applicable.

## Release process overview

```{mermaid}
flowchart LR
    A["<b>prepare-release.yml</b><br/>validate version<br/>check CI<br/>check duplicate tag<br/>update .version<br/>push to branch"] --> B["<b>CI (ci.yml)</b><br/>runs automatically<br/>on release/** branches"]
    B --> C["<b>release.yml</b><br/>build all platforms<br/>optionally sign macOS/Windows<br/>optionally create draft GitHub release<br/>optionally publish to TestPyPI/PyPI"]

    style A fill:#2d333b,stroke:#539bf5,color:#adbac7
    style B fill:#2d333b,stroke:#e5c07b,color:#adbac7
    style C fill:#2d333b,stroke:#7ee787,color:#adbac7
```

## Release workflow jobs

```{mermaid}
flowchart TD
    prepare[prepare<br/><i>validate version,<br/>check CI, check duplicates</i>]

    prepare --> mac["build-and-sign-mac<br/>ARM"]
    prepare --> macint["build-and-sign-mac-intel<br/>Intel"]
    prepare --> win["build-and-sign-windows"]
    prepare --> lin[build-linux-x86<br/><i>installer + wheels</i>]
    prepare --> linarmw[build-linux-arm-wheels]
    prepare --> linarmi[build-linux-arm-installer]

    mac --> release
    macint --> release
    win --> release
    lin --> release
    linarmw --> release
    linarmi --> release

    release["release<br/>draft GitHub release<br/><i>if draft-release</i>"]

    mac --> testpypi
    macint --> testpypi
    win --> testpypi
    lin --> testpypi
    linarmw --> testpypi
    linarmi --> testpypi

    testpypi["publish-testpypi<br/>TestPyPI<br/><i>if publish-testpypi or publish-pypi</i>"]

    release --> pypi
    testpypi --> pypi

    pypi["publish-pypi<br/>PyPI<br/><i>if publish-pypi</i>"]

    style prepare fill:#2d333b,stroke:#539bf5,color:#adbac7
    style mac fill:#2d333b,stroke:#e5c07b,color:#adbac7
    style macint fill:#2d333b,stroke:#e5c07b,color:#adbac7
    style win fill:#2d333b,stroke:#e5c07b,color:#adbac7
    style lin fill:#2d333b,stroke:#e5c07b,color:#adbac7
    style linarmw fill:#2d333b,stroke:#e5c07b,color:#adbac7
    style linarmi fill:#2d333b,stroke:#e5c07b,color:#adbac7
    style release fill:#2d333b,stroke:#7ee787,color:#adbac7
    style testpypi fill:#2d333b,stroke:#7ee787,color:#adbac7
    style pypi fill:#2d333b,stroke:#7ee787,color:#adbac7
```

## Inputs

**prepare-release:** takes a `version`, the workflow ref, and an optional
`skip-ci-check` flag.

**release:** takes a `version` (must match `.version` for public release
operations) and five boolean inputs:

- `sign` signs macOS and Windows artifacts.
- `draft-release` creates the draft GitHub release.
- `publish-testpypi` publishes wheels to TestPyPI.
- `publish-pypi` publishes wheels to PyPI.
- `skip-ci-check` skips the CI status check.

All booleans default to `false`. Non-release runs use the `.version`
already in the repo, so builds work without a prepare step.

For a normal public release, enable the first four booleans: `sign=true`,
`draft-release=true`, `publish-testpypi=true`, and `publish-pypi=true`.

## Environment gates

The release workflow uses GitHub
[environments](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment)
as manual approval gates. Jobs that access signing credentials or publish
artifacts require a reviewer to approve the deployment before they run:

- **`release`** — Required when `sign`, `draft-release`, `publish-testpypi`, or
  `publish-pypi` is enabled. Protects code-signing secrets, the release token,
  and PyPI/TestPyPI trusted publishing/OIDC.

When `sign` is disabled, the macOS and Windows build jobs run without the
`release` environment so they do not require approval and cannot access signing
secrets.

## Workflow input behavior

The `release.yml` workflow uses independent boolean inputs to control what gets
signed and published:

| Input              | Effect                                                                                                                                                                                                                                            |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sign`             | Signs macOS and Windows artifacts. Requires the `release` environment. When false, those jobs upload unsigned artifacts and do not access signing secrets.                                                                                        |
| `draft-release`    | Creates a draft GitHub release with generated release notes and installer artifacts. Requires passing CI (unless skipped), no duplicate tag/release, and `version` matching `.version`. Unsigned fork drafts receive an automatic build suffix.   |
| `publish-testpypi` | Publishes wheels to TestPyPI. Requires the `release` environment.                                                                                                                                                                                 |
| `publish-pypi`     | Publishes wheels to PyPI. Requires the `release` environment, passing CI (unless skipped), and `version` matching `.version`. It also runs and waits for the TestPyPI publish job first. It does not require signing unless `draft-release=true`. |
| `skip-ci-check`    | Skips the CI status check. Useful for hotfix releases.                                                                                                                                                                                            |
| `version`          | For `draft-release` or `publish-pypi`: must match `.version`. For build-only, signed-only, or TestPyPI-only runs: ignored (`.version` from the branch is used automatically).                                                                     |

## Running releases with just

The `release` module in `release.just` wraps both workflow dispatches. Release
recipes take an explicit `--ref` pointing at the commit or branch to build.

Run `just --list --list-submodules` to see all available recipes and their
arguments.

## Testing the release workflow from a feature branch

`release.yml` can be dispatched from any branch for testing. The release guards
only apply when `draft-release` or `publish-pypi` is enabled. To run a test
build:

1. Dispatch `release.yml` from your branch with all boolean inputs left false:
    ```
    just release::build --ref <your-branch>
    ```
2. The workflow reads `.version` from the branch as-is (the version input is
   ignored for non-release runs), so no prepare step is needed.
3. All release guards (CI check, duplicate tag check) are skipped.
4. Artifacts are uploaded to the workflow run but nothing is published or tagged.

### Testing with code signing

To test the signing flow from a feature branch:

1. In the repo's Settings → Environments → `release`, temporarily add your
   branch to the allowed deployment branches.
2. Dispatch the workflow:
    ```
    just release::sign --ref <your-branch>
    ```
3. Approve the environment deployment when prompted.
4. After testing, remove your branch from the environment's allowed branches.

> **Note:** `workflow_dispatch` workflows only appear in the GitHub Actions UI
> if the workflow file exists on the default branch. If `release.yml` is new or
> modified on your branch, use `gh workflow run` to trigger it — the UI
> dropdown won't show it until it's merged to main.

## Important notes

- The release workflow builds the exact commit at `github.sha`. It does not
  write `.version` — that is done by the prepare script. If you dispatch release
  before the prepare commit has been pushed, the build will use whatever
  `.version` was HEAD at dispatch time.
- `just release::draft` intentionally creates unsigned fork builds. It assigns
  the GitHub Actions run number as the build number and keeps that build number
  in the release tag rather than the application version.
- When `publish-pypi=true`, wheels are published to TestPyPI first, then to
  PyPI after the TestPyPI job succeeds. If `draft-release=true` is also set,
  PyPI publishing waits for the draft GitHub release to succeed too.
- Pre-release versions (e.g. `26.05b1`) are automatically marked as
  pre-releases on the GitHub draft release.

## Announcements

- Once a GitHub release draft is created, modify the generated changelog if necessary then click **Publish release**.
- Create a forum topic on the [Beta Testing](https://forums.ankiweb.net/c/anki/beta-testing/13) category. For stable releases, lock the topic and ask users to report issues on a new topic.
- For stable releases, update the version in [ankitects/anki-landing-page](https://github.com/ankitects/anki-landing-page) (See [example](https://github.com/ankitects/anki-landing-page/commit/2362eb2202f174df2aad1dc5336e1b5195a7af85)).

<!-- <<<end>>> -->

