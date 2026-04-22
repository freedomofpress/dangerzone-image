#!/bin/bash

set -e
set -x

SCRIPT_DIR="$( dirname -- "$0" )"
ROOT_DIR=${SCRIPT_DIR}/../
RPMBUILD_DIR=${ROOT_DIR}/../rpmbuild/

cd $ROOT_DIR
rpmbuild -ba -v --build-in-place ${SCRIPT_DIR}/dangerzone-insecure-converter.spec

echo "Copying RPMs under ./qubes/dist/"
cp -v ${RPMBUILD_DIR}/RPMS/**/dangerzone-insecure-converter* ${RPMBUILD_DIR}SRPMS/dangerzone-insecure-converter* ${SCRIPT_DIR}/dist
