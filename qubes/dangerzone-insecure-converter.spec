################################################################################
# Dangerzone RPM SPEC
#
# This SPEC file describes how `rpmbuild` can package Dangerzone into an RPM
# file. It follows the most recent (as of writing this) Fedora guidelines on
# packaging a Python project:
#
#     https://docs.fedoraproject.org/en-US/packaging-guidelines/Python/

################################################################################
# Package Description

Name:           dangerzone-insecure-converter-qubes
Version:        1.0.0
Release:        1%{?dist}
Summary:        Internal (INSECURE!) Dangerzone document to pixels conversion
License:        AGPL-3.0
URL:            https://dangerzone.rocks
###Source0:        https://github.com/freedomofpress/dangerzone-image/archive/refs/tags/{version}.tar.gz
BuildArch:      noarch

# TODO: Check what's the actual description that's recorded in the RPM package.
%description
Internal (INSECURE!) Dangerzone document to pixels conversion

################################################################################
# Package Requirements

# Base requirement for every Python package.
BuildRequires:  python3-devel

# Runtime requirements for converting Office documents
Requires:       libreoffice

################################################################################
# Package Build Instructions

%prep
%autosetup -p1 -n dangerzone-insecure-converter-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l dangerzone_insecure_converter

install -pm 755 -d %{buildroot}/etc/qubes-rpc
install -pm 755 qubes/qubes-rpc/* %{buildroot}/etc/qubes-rpc

%check
%pyproject_check_import

# Detect if the filesystem has been affecting our file permissions.
bad_files=$(find %{buildroot} -perm 0600)
if [ -n "${bad_files}" ]; then
    echo "Error while building the Dangerzone RPM. Detected the following files with wrong permissions (600):"
    echo ${bad_files}
    echo ""
    echo "For more info about this error, see https://github.com/freedomofpress/dangerzone/issues/727"
    exit 1
fi

%files -f %{pyproject_files}
%license LICENSE
%doc README.md

/etc/qubes-rpc

%changelog
%autochangelog
