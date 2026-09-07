#!/bin/sh
# scripts/publish.sh -- tag the current commit as a release and push it to
# GitHub. Building and attaching the .deb/.pkg is NOT done here -- that is
# GitHub Actions' job (.github/workflows/release.yml), triggered by the tag
# push below. Run this from a clean, up-to-date main branch.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- sanity checks -----------------------------------------------------

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "${BRANCH}" != "main" ]; then
    echo "ERROR: not on 'main' (currently on '${BRANCH}') -- aborting" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is not clean -- commit or stash your changes first" >&2
    git status --short
    exit 1
fi

if ! git remote get-url origin > /dev/null 2>&1; then
    echo "ERROR: no 'origin' remote configured." >&2
    echo "       git remote add origin git@github.com:<user>/milter-mime-filter.git" >&2
    exit 1
fi

VERSION="$(tr -d '[:space:]' < VERSION)"
if [ -z "${VERSION}" ]; then
    echo "ERROR: VERSION file is empty" >&2
    exit 1
fi

# VERSION and the code's own __version__ are two separate places (by
# design -- see REFACTOR_BRIEF.md); refuse to publish if they drift apart.
CODE_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/mail_filter/__init__.py)"
if [ "${VERSION}" != "${CODE_VERSION}" ]; then
    echo "ERROR: VERSION file says ${VERSION}, but src/mail_filter/__init__.py says ${CODE_VERSION}" >&2
    echo "       Update both to the same value before publishing." >&2
    exit 1
fi

TAG="v${VERSION}"

if git rev-parse "${TAG}" > /dev/null 2>&1; then
    echo "ERROR: tag ${TAG} already exists -- bump VERSION first" >&2
    exit 1
fi

# --- publish -------------------------------------------------------------

echo "Publishing ${TAG} from branch ${BRANCH}..."
git push origin "${BRANCH}"
git tag -a "${TAG}" -m "mail_filter ${VERSION}"
git push origin "${TAG}"

echo
echo "Pushed. GitHub Actions will now build the .deb and .pkg and attach"
echo "them to a new Release for ${TAG} -- check the Actions tab, then Releases."
