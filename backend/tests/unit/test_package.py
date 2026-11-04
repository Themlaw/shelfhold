import shelfhold


def test_package_exposes_semver_version() -> None:
    major, minor, patch = shelfhold.__version__.split(".")
    assert (major + minor + patch).isdigit()
