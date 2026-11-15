#!/bin/bash
: '=======================================================
Release (non-Python)

Stages, commits, tags and pushes a new release of a non-Python
project to git. Version tracking is done via git tags only.

Usage: nopy_release.sh <version> <comment>

The release is tagged v<version>. All checks that can fail are done
before anything is changed, and the script refuses to run if tag
v<version> already exists locally or on origin.
=========================================================='

# set -euo pipefail

RELEASE_BRANCH="main"

# Get project name from the git repo directory name
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [ $? -ne 0 ]; then
    echo "Error: Not a git repository."
    exit 1
fi
PROJECT_NAME=$(basename "$REPO_ROOT")

# Get the current version from the latest git tag
CURRENT_VERSION=$(git tag --sort=-v:refname 2>/dev/null | head -1)
if [ -z "$CURRENT_VERSION" ]; then
    CURRENT_VERSION="(none)"
fi

# Check arguments
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <version> <comment>"
    exit 1
fi
VERSION=${1#v}   # accept "1.2.3" or "v1.2.3"
COMMENT=$2
TAG="v$VERSION"

if [ -z "$VERSION" ] || ! git check-ref-format "refs/tags/$TAG"; then
    echo "Error: '$1' is not a valid version for a git tag."
    exit 1
fi

# Make sure we are releasing from the release branch
CURRENT_BRANCH=$(git symbolic-ref --quiet --short HEAD 2>/dev/null)
if [ $? -ne 0 ]; then
    echo "Error: HEAD is detached."
    exit 1
fi
if [ "$CURRENT_BRANCH" != "$RELEASE_BRANCH" ]; then
    echo "Error: releases must be made from '$RELEASE_BRANCH' (current branch is '$CURRENT_BRANCH')."
    exit 1
fi

# Make sure the release tag doesn't already exist, locally or on origin
if git rev-parse --quiet --verify "refs/tags/$TAG" > /dev/null; then
    echo "Error: tag $TAG already exists locally. Choose a new version number."
    exit 1
fi
REMOTE_TAG=$(git ls-remote --tags origin "refs/tags/$TAG")
if [ $? -ne 0 ]; then
    echo "Error: could not query tags on origin."
    exit 1
fi
if [ -n "$REMOTE_TAG" ]; then
    echo "Error: tag $TAG already exists on origin. Choose a new version number."
    exit 1
fi

echo "Project:         $PROJECT_NAME"
echo "Current version: $CURRENT_VERSION"
echo "New version:     $TAG"
echo "Comment:         $COMMENT"
echo
read -p "Enter Y to continue, any other key to abort: " CONFIRM

if [[ "$CONFIRM" != "Y" && "$CONFIRM" != "y" ]]; then
    echo "Aborted."
    exit 0
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
    echo "Nothing to commit; tagging the current HEAD."
else
    echo "Committing changes..."
    git commit -m "$COMMENT"
    if [ $? -ne 0 ]; then
        echo "Error: git commit failed."
        exit 1
    fi
fi

# Tag the new release
echo "Tagging release $TAG..."
git tag "$TAG"
if [ $? -ne 0 ]; then
    echo "Error: git tag $TAG failed."
    exit 1
fi

# Push to origin
echo "Pushing to origin $RELEASE_BRANCH..."
git push origin "$RELEASE_BRANCH"
if [ $? -ne 0 ]; then
    echo "Error: git push origin $RELEASE_BRANCH failed. Tag $TAG was created locally but not pushed."
    exit 1
fi

echo "Pushing to origin $TAG..."
git push origin "$TAG"
if [ $? -ne 0 ]; then
    echo "Error: git push origin $TAG failed. Retry with: git push origin $TAG"
    exit 1
fi

echo "Release $TAG committed and pushed with comment: $COMMENT"
