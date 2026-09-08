#!/bin/sh
# build_pkg.sh -- build a FreeBSD pkg package for mail_filter.
#
# Run from anywhere; the script locates the repository root relative to
# its own path. Nothing here is version-specific -- this file is not
# copied per release, and the version comes from the repository's single
# VERSION file, never from a directory name or a hardcoded value.
#
# The files{} section of +MANIFEST is generated from the staged tree
# itself (see below), not hand-written -- a file that is staged but
# missing from files{} will simply never happen.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PKGNAME="mail_filter"

VERSION_FILE="${REPO_ROOT}/VERSION"
if [ ! -s "${VERSION_FILE}" ]; then
    echo "ERROR: ${VERSION_FILE} not found or empty -- refusing to guess a version" >&2
    exit 1
fi
VERSION=$(tr -d '[:space:]' < "${VERSION_FILE}")

BUILD_DIR="${REPO_ROOT}/build/freebsd"
STAGEDIR="${BUILD_DIR}/${PKGNAME}-${VERSION}"
OUTDIR="${REPO_ROOT}/dist"

rm -rf "${STAGEDIR}"
mkdir -p "${STAGEDIR}/usr/local/sbin" \
         "${STAGEDIR}/usr/local/lib/mail_filter" \
         "${STAGEDIR}/usr/local/libexec/mail_filter" \
         "${STAGEDIR}/usr/local/etc/mail_filter" \
         "${STAGEDIR}/usr/local/etc/rc.d" \
         "${STAGEDIR}/etc/newsyslog.conf.d" \
         "${STAGEDIR}/usr/local/share/man/man1" \
         "${STAGEDIR}/usr/local/share/man/man5" \
         "${STAGEDIR}/usr/local/share/man/man8" \
         "${OUTDIR}"

# --- stage the shared, platform-independent tree ---------------------------

# hier(7): /usr/local/sbin is for "local administration utilities",
# /usr/local/lib for "local libraries". mail_filter.py's own package of
# importable modules is a library, not a utility in its own right, so it
# (and the launcher next to it, per mail_filter.py's own docstring) lives
# under lib/ -- matching real precedent already on this build host, e.g.
# dovecot ships its own files under /usr/local/lib/dovecot/, not sbin/.
# A thin wrapper at /usr/local/sbin/mail_filter (see mail_filter-wrapper)
# is what admins actually type.
cp "${REPO_ROOT}/src/mail_filter.py" "${STAGEDIR}/usr/local/lib/mail_filter/mail_filter.py"
cp -r "${REPO_ROOT}/src/mail_filter" "${STAGEDIR}/usr/local/lib/mail_filter/mail_filter"
install -m 0755 "${SCRIPT_DIR}/mail_filter-wrapper" "${STAGEDIR}/usr/local/sbin/mail_filter"
cp "${REPO_ROOT}/src/libexec/check_regex.py" "${STAGEDIR}/usr/local/libexec/mail_filter/check_regex.py"
cp "${REPO_ROOT}/src/libexec/generate_emoji_regex.py" "${STAGEDIR}/usr/local/libexec/mail_filter/generate_emoji_regex.py"

for f in mail_filter.conf mail_filter_rules.conf mail_filter_whitelist.cidr emoji_regex.inc; do
    cp "${REPO_ROOT}/conf/${f}" "${STAGEDIR}/usr/local/etc/mail_filter/${f}.sample"
done

cp "${REPO_ROOT}/man/man1/generate_emoji_regex.py.1" "${STAGEDIR}/usr/local/share/man/man1/"
cp "${REPO_ROOT}/man/man1/check_regex.py.1" "${STAGEDIR}/usr/local/share/man/man1/"
cp "${REPO_ROOT}/man/man5/mail_filter.conf.5" "${STAGEDIR}/usr/local/share/man/man5/"
cp "${REPO_ROOT}/man/man5/mail_filter_rules.conf.5" "${STAGEDIR}/usr/local/share/man/man5/"
cp "${REPO_ROOT}/man/man5/mail_filter_whitelist.cidr.5" "${STAGEDIR}/usr/local/share/man/man5/"
cp "${REPO_ROOT}/man/man8/mail_filter.8" "${STAGEDIR}/usr/local/share/man/man8/"

# --- stage the FreeBSD-specific pieces ---------------------------------------

cp "${SCRIPT_DIR}/rc.d/mail_filter" "${STAGEDIR}/usr/local/etc/rc.d/mail_filter"
cp "${SCRIPT_DIR}/newsyslog.conf.d/mail_filter.conf" "${STAGEDIR}/etc/newsyslog.conf.d/mail_filter.conf"

find "${STAGEDIR}" -name '__pycache__' -type d -prune -exec rm -rf {} +

# --------------------------------------------------------------------------
# Python version / pymilter dependency name detection
#
# Priority (highest first):
#   1. rc.conf on this build host -- an admin's explicit override
#   2. the highest-versioned /usr/local/bin/python3.N found directly
#
# Deliberately does NOT fall back to `which python3`: an unversioned
# python3 resolving via PATH is exactly the unguaranteed assumption this
# whole exercise exists to remove (see the pkg message below), so the build
# host should not depend on it either.
# --------------------------------------------------------------------------

PYTHON=""

if [ -f /etc/rc.conf ]; then
    _rc=$(grep 'mail_filter_python' /etc/rc.conf 2>/dev/null \
          | tail -1 | sed 's/.*="\(.*\)".*/\1/')
    [ -n "${_rc}" ] && [ -x "${_rc}" ] && PYTHON="${_rc}"
fi

if [ -z "${PYTHON}" ]; then
    PYTHON=$(ls /usr/local/bin/python3.[0-9]* 2>/dev/null | grep -v -- '-config$' | sort -V | tail -1)
fi

if [ -z "${PYTHON}" ] || [ ! -x "${PYTHON}" ]; then
    echo "ERROR: no /usr/local/bin/python3.N interpreter found -- install one (e.g. pkg install python312) before building" >&2
    exit 1
fi

PYVER=$(${PYTHON} -c "import sys; print('%d%d' % sys.version_info[:2])")
PYVER_DOTTED=$(${PYTHON} -c "import sys; print('%d.%d' % sys.version_info[:2])")
PYMILTER_PKG="py${PYVER}-pymilter"

# The actual, pkg-guaranteed versioned interpreter -- e.g. /usr/local/bin/python3.12,
# installed by lang/python312 (a dependency of ${PYMILTER_PKG}). This is what gets
# baked into rc.d below, NOT ${PYTHON} above, which may itself just be the
# unversioned /usr/local/bin/python3 symlink -- an admin convenience some FreeBSD
# installs have and others don't, never guaranteed by any port.
PYMILTER_PYTHON="/usr/local/bin/python${PYVER_DOTTED}"
if [ ! -x "${PYMILTER_PYTHON}" ]; then
    echo "ERROR: expected interpreter not found or not executable: ${PYMILTER_PYTHON}" >&2
    exit 1
fi

echo "Building ${PKGNAME}-${VERSION}.pkg"
echo "  Repo     : ${REPO_ROOT}"
echo "  Staging  : ${STAGEDIR}"
echo "  Output   : ${OUTDIR}"
echo "  Python   : ${PYMILTER_PYTHON} (${PYVER_DOTTED})"
echo "  Dep pkg  : ${PYMILTER_PKG}"
echo

if ! pkg info "${PYMILTER_PKG}" > /dev/null 2>&1; then
    echo "WARNING: ${PYMILTER_PKG} is not installed on this build host."
    echo "         The milter will not work without it."
    echo "         Install with:  pkg install ${PYMILTER_PKG}"
    echo
fi

# rc.d's default and the entrypoints' own #!/usr/bin/env python3 shebang are
# both left generic (not rewritten to a concrete path here) -- an unversioned
# /usr/local/bin/python3 is not guaranteed to exist, but which exact version
# this build needs IS known (computed above), so pkg-post-install checks at
# install time whether the generic shebang actually resolves to something
# working and tells the admin the one command to fix it, instead of this
# script silently baking in a path that may go stale after a Python upgrade.

# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------

echo '--- setting permissions ---'
find "${STAGEDIR}" -type d -exec chmod 0755 {} +
chmod 0755 "${STAGEDIR}/usr/local/sbin/mail_filter"
chmod 0755 "${STAGEDIR}/usr/local/lib/mail_filter/mail_filter.py"
find "${STAGEDIR}/usr/local/lib/mail_filter/mail_filter" -name '*.py' -exec chmod 0644 {} +
chmod 0555 "${STAGEDIR}/usr/local/libexec/mail_filter/generate_emoji_regex.py"
chmod 0555 "${STAGEDIR}/usr/local/libexec/mail_filter/check_regex.py"
chmod 0755 "${STAGEDIR}/usr/local/etc/rc.d/mail_filter"
# 0644 rather than 0640, because the directory above is 0750 root:postfix --
# nothing outside root and the daemon can reach these files whatever their
# mode, so the group bit protects nothing and costs something. cp preserves
# the mode of its source (verified on FreeBSD and on Debian; umask does not
# loosen it back), so a 0640 sample turns
#
#     cp mail_filter.conf.sample mail_filter.conf
#
# -- the obvious thing for an administrator to do -- into a file the daemon
# cannot read, because it runs as postfix and the copy is root:wheel. At
# 0644 the same command produces a working file.
chmod 0644 "${STAGEDIR}"/usr/local/etc/mail_filter/*.sample
chmod 0644 "${STAGEDIR}/etc/newsyslog.conf.d/mail_filter.conf"
chmod 0444 "${STAGEDIR}/usr/local/share/man/man8/mail_filter.8"
chmod 0444 "${STAGEDIR}/usr/local/share/man/man5/mail_filter.conf.5"
chmod 0444 "${STAGEDIR}/usr/local/share/man/man5/mail_filter_rules.conf.5"
chmod 0444 "${STAGEDIR}/usr/local/share/man/man5/mail_filter_whitelist.cidr.5"
chmod 0444 "${STAGEDIR}/usr/local/share/man/man1/generate_emoji_regex.py.1"
chmod 0444 "${STAGEDIR}/usr/local/share/man/man1/check_regex.py.1"

# --------------------------------------------------------------------------
# flatsize
# --------------------------------------------------------------------------

FLATSIZE=$(find "${STAGEDIR}" ! -name '+MANIFEST' \
    -type f | xargs stat -f '%z' | awk '{s+=$1} END{print s+0}')

# --------------------------------------------------------------------------
# +MANIFEST -- static part from the template, files{} generated from the
# staged tree itself so a staged file can never be missing from it.
#
# Install/deinstall scripts are embedded directly in the manifest via
# scripts{} (pkg-create(8)'s modern mechanism) -- a loose +POST-INSTALL /
# +PRE-DEINSTALL file next to +MANIFEST is only picked up by `pkg create
# -i metadatadir`, which is incompatible with the -M mode used here, so it
# would silently never run. Verified empirically against this pkg version.
# --------------------------------------------------------------------------

sed -e "s/@VERSION@/${VERSION}/" \
    -e "s/@FLATSIZE@/${FLATSIZE}/" \
    -e "s/@PYMILTER_PKG@/${PYMILTER_PKG}/g" \
    "${SCRIPT_DIR}/MANIFEST.template" > "${STAGEDIR}/+MANIFEST"

POST_INSTALL_TMP=$(mktemp)
sed -e "s#@PYTHON_PATH@#${PYMILTER_PYTHON}#" \
    -e "s/@PYMILTER_PKG@/${PYMILTER_PKG}/g" \
    "${SCRIPT_DIR}/pkg-post-install" > "${POST_INSTALL_TMP}"

INSTALL_SCRIPT_JSON=$(${PYTHON} -c "import json,sys; print(json.dumps(open(sys.argv[1]).read()))" "${POST_INSTALL_TMP}")
DEINSTALL_SCRIPT_JSON=$(${PYTHON} -c "import json,sys; print(json.dumps(open(sys.argv[1]).read()))" "${SCRIPT_DIR}/pkg-pre-deinstall")
POST_DEINSTALL_SCRIPT_JSON=$(${PYTHON} -c "import json,sys; print(json.dumps(open(sys.argv[1]).read()))" "${SCRIPT_DIR}/pkg-post-deinstall")
rm -f "${POST_INSTALL_TMP}"

{
    echo "scripts: {"
    echo "  install: ${INSTALL_SCRIPT_JSON};"
    echo "  pre-deinstall: ${DEINSTALL_SCRIPT_JSON};"
    echo "  post-deinstall: ${POST_DEINSTALL_SCRIPT_JSON};"
    echo "}"
} >> "${STAGEDIR}/+MANIFEST"

echo "files {" >> "${STAGEDIR}/+MANIFEST"
find "${STAGEDIR}" \
    ! -name '+MANIFEST' \
    -type f -print \
    | while IFS= read -r f; do
        rel="${f#${STAGEDIR}}"
        echo "  \"${rel}\" = \"-\";" >> "${STAGEDIR}/+MANIFEST"
    done
echo "}" >> "${STAGEDIR}/+MANIFEST"

echo
echo '--- +MANIFEST ---'
cat "${STAGEDIR}/+MANIFEST"

# --------------------------------------------------------------------------
# Build the package
# --------------------------------------------------------------------------

echo
echo '--- building pkg ---'
pkg create -M "${STAGEDIR}/+MANIFEST" \
           -r "${STAGEDIR}" \
           -o "${OUTDIR}"

echo
echo "  Package : ${OUTDIR}/${PKGNAME}-${VERSION}.pkg"
echo "  Size    : $(du -sh "${OUTDIR}/${PKGNAME}-${VERSION}.pkg" | cut -f1)"
echo
echo "  NOTE: pkg add does NOT auto-install dependencies."
echo "        Install the required dependency first:"
echo "          pkg install ${PYMILTER_PKG}"
echo "        Then install this package:"
echo "          pkg add ${OUTDIR}/${PKGNAME}-${VERSION}.pkg"
echo
echo "  Remove  : pkg delete ${PKGNAME}"
echo "  Info    : pkg info ${PKGNAME}"
