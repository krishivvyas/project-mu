#!/usr/bin/env bash
# Project Mu (無) — add one platform's files to this run's GitHub release.
#
# Usage: publish_release.sh DIR
#   DIR holds the files to publish, each with a matching <file>.sha256.
# Env: GH_TOKEN, GITHUB_REPOSITORY, GITHUB_SHA, VERSION, RUN.
#
# The Windows and Android chains finish at different times, so whichever
# publishes first creates the release and the other adds its files to it.
# Asset names carry no version, so the website can always link to
# releases/latest/download/<name>.
set -euo pipefail

dir=$1
tag="mu-$VERSION-r$RUN"
repo=$GITHUB_REPOSITORY

cd "$dir"
sha256sum -c ./*.sha256
mapfile -t files < <(ls -1)

notes=$(mktemp)
cat > "$notes" <<EOF
Project Mu built from Chromium **$VERSION** with the patch set at $GITHUB_SHA.

| Platform | File |
|---|---|
| Windows 10/11 x64, installer | \`project-mu-windows-x64-setup.exe\` |
| Windows 10/11 x64, portable | \`project-mu-windows-x64-portable.zip\` (unzip and run \`chrome.exe\`) |
| Android arm64 | \`project-mu-android-arm64.apk\` |

A platform missing from this release did not finish building in this run.
Verify each download against its \`.sha256\` file.
EOF

if gh release view "$tag" --repo "$repo" > /dev/null 2>&1; then
  gh release upload "$tag" "${files[@]}" --repo "$repo" --clobber
elif ! gh release create "$tag" "${files[@]}" --repo "$repo" --target "$GITHUB_SHA" \
       --title "Project Mu $VERSION (build $RUN)" --notes-file "$notes"; then
  # The other platform created the release a moment ago.
  gh release upload "$tag" "${files[@]}" --repo "$repo" --clobber
fi
echo "Published ${files[*]} to [$tag](${GITHUB_SERVER_URL:-https://github.com}/$repo/releases/tag/$tag)" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
