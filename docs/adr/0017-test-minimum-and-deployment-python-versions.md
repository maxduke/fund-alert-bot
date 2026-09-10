# Test the minimum and deployment Python versions

Deploy the standard CPython 3.14 Docker image while retaining Python 3.12 as the
package's minimum supported version and Ruff language target. Test both versions
with the same pinned dependencies, and require an offline startup/shutdown and
native-dependency smoke test of the built image before merging runtime updates;
this makes the deployed interpreter part of validation without unnecessarily
dropping existing local Python 3.12 installations.
