#!/bin/bash

set -e
set -x

SCRIPT_DIR="$( realpath -- $( dirname -- "$0" ) )"
ROOT_DIR="$( realpath -- ${SCRIPT_DIR}/../ )"
RPMBUILD_DIR=~/rpmbuild/

cd $ROOT_DIR
rpmbuild -ba -v --build-in-place ${SCRIPT_DIR}/dangerzone-insecure-converter.spec

echo "Copying RPMs under ./qubes/dist/"
cp -v \
    ${RPMBUILD_DIR}/RPMS/**/dangerzone-insecure-converter*.noarch.rpm \
    ${RPMBUILD_DIR}/SRPMS/dangerzone-insecure-converter*.src.rpm \
    ${SCRIPT_DIR}/dist
