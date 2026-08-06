#!/bin/bash

set -e
set -x

SCRIPT_DIR="$( realpath -- $( dirname -- "$0" ) )"
ROOT_DIR="$( realpath -- ${SCRIPT_DIR}/../ )"
RPMBUILD_DIR=~/rpmbuild/

mkdir -p ${RPMBUILD_DIR?}/{SOURCES,SPECS}
cd $ROOT_DIR

# Grab the version of the project from pyproject.toml.
VERSION="$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")"

# Create a source tarball using git, and prefix it with the version that
# `rpmbuild` expects.
git archive \
    --prefix dangerzone-insecure-converter-${VERSION?}/ \
    -o ${RPMBUILD_DIR?}/SOURCES/dangerzone-image-${VERSION?}.tar.gz \
    HEAD

# Copy the spec file to the SPECS dir that `rpmbuild` expects.
cp ${SCRIPT_DIR?}/dangerzone-insecure-converter.spec ${RPMBUILD_DIR?}/SPECS/

# Run `rpmbuild` and create both a binary and source RPM.
rpmbuild -ba -v ${SCRIPT_DIR}/dangerzone-insecure-converter.spec

echo "Copying RPMs under ./qubes/dist/"
cp -v \
    ${RPMBUILD_DIR}/RPMS/**/dangerzone-insecure-converter*.noarch.rpm \
    ${RPMBUILD_DIR}/SRPMS/dangerzone-insecure-converter*.src.rpm \
    ${SCRIPT_DIR}/dist
