#!/bin/bash
: '=======================================================
Release

Stages, commits, tags and pushes a new release of the project to git.

Usage: release.sh [--force] <version> <comment>

The version in pyproject.toml is set to <version> (it may already be
set, e.g. if it was bumped by hand) and the release is tagged
v<version>. All checks that can fail are done before anything is
changed, and the script refuses to run if tag v<version> already
exists locally or on origin, unless --force (-f) is given. With
--force, a version that is already tagged is re-released: the changes
are committed and pushed, and the existing tag is left as is.
=========================================================='

# set -euo pipefail

PYPROJECT="pyproject.toml"
RELEASE_BRANCH="main"

# Print the value of a key in the [project] table of pyproject.toml.
read_project_key() {
    sed -n -E '/^\[project\]/,/^\[/ s/^'"$1"' *= *"([^"]+)".*$/\1/p' "$PYPROJECT" | head -1
}

# Get the current version from pyproject.toml, or hand off to nopy_release.sh
if [ -f "$PYPROJECT" ]; then
    CURRENT_VERSION=$(read_project_key version)
    PROJECT_NAME=$(read_project_key name)
else
    NOPY_SCRIPT="$(dirname "$0")/nopy_release.sh"
    if [ ! -f "$NOPY_SCRIPT" ]; then
        echo "Error: $PYPROJECT not found and $NOPY_SCRIPT is missing."
        exit 1
    fi
    exec "$NOPY_SCRIPT" "$@"
fi

if [ -z "$CURRENT_VERSION" ]; then
    echo "Error: version not defined in the [project] table of $PYPROJECT."
    exit 1
fi

# Check arguments
FORCE=false
ARGS=()
for ARG in "$@"; do
    case "$ARG" in
        -f|--force) FORCE=true ;;
        *) ARGS+=("$ARG") ;;
    esac
done
if [ "${#ARGS[@]}" -ne 2 ]; then
    echo "Usage: $0 [--force] <version> <comment>"
    echo "  -f, --force  Allow re-releasing a version that is already tagged: changes"
    echo "               are committed and pushed, and the existing tag is left as is."
    exit 1
fi
VERSION=${ARGS[0]#v}   # accept "1.2.3" or "v1.2.3"
COMMENT=${ARGS[1]}
TAG="v$VERSION"

if [ -z "$VERSION" ] || ! git check-ref-format "refs/tags/$TAG"; then
    echo "Error: '${ARGS[0]}' is not a valid version for a git tag."
    exit 1
fi

# Make sure we are releasing from the release branch of a git repo
CURRENT_BRANCH=$(git symbolic-ref --quiet --short HEAD 2>/dev/null)
if [ $? -ne 0 ]; then
    echo "Error: not in a git repository, or HEAD is detached."
    exit 1
fi
if [ "$CURRENT_BRANCH" != "$RELEASE_BRANCH" ]; then
    echo "Error: releases must be made from '$RELEASE_BRANCH' (current branch is '$CURRENT_BRANCH')."
    exit 1
fi

# Check whether the release tag already exists, locally or on origin. That is
# an error unless --force was given, in which case this is a re-release: the
# changes are committed and pushed, but the existing tag is never moved.
TAG_LOCAL=false
if git rev-parse --quiet --verify "refs/tags/$TAG" > /dev/null; then
    TAG_LOCAL=true
fi
REMOTE_TAG=$(git ls-remote --tags origin "refs/tags/$TAG")
if [ $? -ne 0 ]; then
    echo "Error: could not query tags on origin."
    exit 1
fi
TAG_REMOTE=false
if [ -n "$REMOTE_TAG" ]; then
    TAG_REMOTE=true
fi
RERELEASE=false
if [ "$TAG_LOCAL" = true ] || [ "$TAG_REMOTE" = true ]; then
    if [ "$FORCE" != true ]; then
        if [ "$TAG_LOCAL" = true ]; then WHERE="locally"; else WHERE="on origin"; fi
        echo "Error: tag $TAG already exists $WHERE. Choose a new version number, or use --force to re-release $TAG without re-tagging."
        exit 1
    fi
    RERELEASE=true
fi

# Make sure our virtual environment is activated
if [ -z "$VIRTUAL_ENV" ]; then
    echo "Error: No virtual environment activated."
    exit 1
fi

# Check if there is a tests/ folder to determine if pytest is needed
if [ -d "tests" ]; then
    PYTEST_NEEDED=true
    # Make sure pytest is in the path
    if ! command -v pytest &> /dev/null; then
        echo "Error: pytest could not be found. Please install it in your virtual environment."
        exit 1
    fi
else
    PYTEST_NEEDED=false
fi

# Check if there is a mkdocs.yml file to determine if documentation build is needed
if [ -f "mkdocs.yml" ]; then
    DOCS_NEEDED=true
    # Make sure mkdocs is in the path
    if ! command -v mkdocs &> /dev/null; then
        echo "Error: mkdocs could not be found. Please install it in your virtual environment."
        exit 1
    fi
else
    DOCS_NEEDED=false
fi


echo "Project:         $PROJECT_NAME"
echo "Current version: $CURRENT_VERSION"
echo "New version:     $VERSION"
if [ "$VERSION" = "$CURRENT_VERSION" ] && [ "$RERELEASE" = false ]; then
    echo "                 ($PYPROJECT is already at this version; it will still be tagged $TAG)"
fi
echo "Comment:         $COMMENT"
if [ "$RERELEASE" = true ]; then
    echo "Re-release:      tag $TAG already exists and will be left as is (--force)"
fi
echo
read -p "Enter Y to continue, any other key to abort: " CONFIRM

if [[ "$CONFIRM" != "Y" && "$CONFIRM" != "y" ]]; then
    echo "Aborted."
    exit 0
fi

# Run tests using pytest and check for errors
if [ "$PYTEST_NEEDED" = true ]; then
    echo "Running tests with pytest..."
    pytest
    if [ $? -ne 0 ]; then
        echo "Error: Tests failed."
        exit 1
    fi
fi

# Build the documentation using mkdocs and check for errors
if [ "$DOCS_NEEDED" = true ]; then
    echo "Building documentation with mkdocs..."
    mkdocs build --clean
    if [ $? -ne 0 ]; then
        echo "Error: mkdocs build failed."
        exit 1
    fi
fi

# Update the version in the [project] table of pyproject.toml
if [ "$VERSION" != "$CURRENT_VERSION" ]; then
    echo "Updating version in $PYPROJECT to $VERSION..."
    sed -i.bak -E '/^\[project\]/,/^\[/ s/^(version *= *")[^"]*(")/\1'"$VERSION"'\2/' "$PYPROJECT"
    SED_STATUS=$?
    rm -f "$PYPROJECT.bak"
    if [ $SED_STATUS -ne 0 ] || [ "$(read_project_key version)" != "$VERSION" ]; then
        echo "Error: failed to update version in $PYPROJECT."
        exit 1
    fi
fi

# Stage all changes
echo "Staging changes..."
git add .
if [ $? -ne 0 ]; then
    echo "Error: git add failed."
    exit 1
fi

# Commit with the provided comment, unless there is nothing to commit
if git diff --cached --quiet; then
    echo "Nothing to commit."
else
    echo "Committing changes..."
    git commit -m "$COMMENT"
    if [ $? -ne 0 ]; then
        echo "Error: git commit failed."
        exit 1
    fi
fi

# Tag the new release, unless this is a re-release of an existing tag
if [ "$TAG_LOCAL" = false ] && [ "$TAG_REMOTE" = false ]; then
    echo "Tagging release $TAG..."
    git tag "$TAG"
    if [ $? -ne 0 ]; then
        echo "Error: git tag $TAG failed."
        exit 1
    fi
else
    echo "Tag $TAG already exists; leaving it as is."
fi

# Push to origin
echo "Pushing to origin $RELEASE_BRANCH..."
git push origin "$RELEASE_BRANCH"
if [ $? -ne 0 ]; then
    echo "Error: git push origin $RELEASE_BRANCH failed. Nothing has been pushed."
    exit 1
fi

# Push the tag, unless origin already has it
if [ "$TAG_REMOTE" = false ]; then
    echo "Pushing to origin $TAG..."
    git push origin "$TAG"
    if [ $? -ne 0 ]; then
        echo "Error: git push origin $TAG failed. Retry with: git push origin $TAG"
        exit 1
    fi
fi

# Push the documentation to the gh-pages branch
if [ "$DOCS_NEEDED" = true ]; then
    echo "Deploying documentation to gh-pages..."
    mkdocs gh-deploy --clean
    if [ $? -ne 0 ]; then
        echo "Error: mkdocs deployment failed."
        exit 1
    fi
fi

if [ "$RERELEASE" = true ]; then
    echo "Re-release of $TAG committed and pushed with comment: $COMMENT (tag $TAG left as is)"
else
    echo "Release $TAG committed and pushed with comment: $COMMENT"
fi
