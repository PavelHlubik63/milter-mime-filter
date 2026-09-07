#!/bin/bash
# build_deb.sh -- build a Debian .deb package for mail-filter.
#
# Run from anywhere; the script locates the repository root relative to
# its own path. Nothing here is version-specific -- this file is not
# copied per release, and the version comes from the repository's single
# VERSION file, never from a directory name or a hardcoded value.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PKG="mail-filter"
ARCH="all"

VERSION_FILE="${REPO_ROOT}/VERSION"
if [ ! -s "${VERSION_FILE}" ]; then
    echo "ERROR: ${VERSION_FILE} not found or empty -- refusing to guess a version" >&2
    exit 1
fi
VERSION="$(tr -d '[:space:]' < "${VERSION_FILE}")"

BUILD_DIR="${REPO_ROOT}/build/debian"
STAGEDIR="${BUILD_DIR}/pkg"
OUTDIR="${REPO_ROOT}/dist"
DEBFILE="${OUTDIR}/${PKG}_${VERSION}_${ARCH}.deb"

if ! command -v dpkg-deb > /dev/null 2>&1; then
    echo "ERROR: dpkg-deb not found -- aborting" >&2
    exit 1
fi

echo "Building ${PKG}_${VERSION}_${ARCH}.deb"
echo "  Repo    : ${REPO_ROOT}"
echo "  Staging : ${STAGEDIR}"
echo "  Output  : ${DEBFILE}"
echo

rm -rf "${STAGEDIR}"
mkdir -p "${STAGEDIR}/DEBIAN" \
         "${STAGEDIR}/usr/sbin" \
         "${STAGEDIR}/usr/lib/mail_filter" \
         "${STAGEDIR}/usr/lib/systemd/system" \
         "${STAGEDIR}/etc/mail_filter" \
         "${STAGEDIR}/etc/logrotate.d" \
         "${STAGEDIR}/usr/share/man/man1" \
         "${STAGEDIR}/usr/share/man/man5" \
         "${STAGEDIR}/usr/share/man/man8" \
         "${STAGEDIR}/usr/share/doc/${PKG}" \
         "${OUTDIR}"

# --- stage the shared, platform-independent tree ---------------------------

# Debian Policy 9.1.1 reserves /usr/local for the system administrator's own
# installs -- package-owned files never go there (unlike the FreeBSD package,
# where /usr/local is the correct, native pkg(8) prefix). The private
# implementation lives under /usr/lib/mail_filter/, matching how Postfix
# itself ships its own internal daemons on Debian (/usr/lib/postfix/sbin/);
# mail_filter.py's own docstring notes it needs the mail_filter/ package
# sitting next to it, so both stay together here. A thin wrapper at
# /usr/sbin/mail_filter (in root's PATH, no .py suffix) is what admins
# actually run -- see mail_filter-wrapper.
cp "${REPO_ROOT}/src/mail_filter.py" "${STAGEDIR}/usr/lib/mail_filter/mail_filter.py"
cp -r "${REPO_ROOT}/src/mail_filter" "${STAGEDIR}/usr/lib/mail_filter/mail_filter"
cp "${REPO_ROOT}/src/libexec/check_regex.py" "${STAGEDIR}/usr/lib/mail_filter/check_regex.py"
cp "${REPO_ROOT}/src/libexec/generate_emoji_regex.py" "${STAGEDIR}/usr/lib/mail_filter/generate_emoji_regex.py"
install -m 0755 "${SCRIPT_DIR}/mail_filter-wrapper" "${STAGEDIR}/usr/sbin/mail_filter"

for f in mail_filter.conf mail_filter_rules.conf mail_filter_whitelist.cidr emoji_regex.inc; do
    cp "${REPO_ROOT}/conf/${f}" "${STAGEDIR}/etc/mail_filter/${f}.dpkg-dist"
done

# conf/mail_filter.conf ships log_timestamp=bsd (the shared source also used
# for the FreeBSD package). Linux/Debian's native syslog convention is
# ISO 8601, so override just that one line for this platform's sample.
sed -i 's/^log_timestamp = bsd$/log_timestamp = iso8601/' \
    "${STAGEDIR}/etc/mail_filter/mail_filter.conf.dpkg-dist"

cp "${REPO_ROOT}/man/man1/generate_emoji_regex.py.1" "${STAGEDIR}/usr/share/man/man1/"
cp "${REPO_ROOT}/man/man1/check_regex.py.1" "${STAGEDIR}/usr/share/man/man1/"
cp "${REPO_ROOT}/man/man5/mail_filter.conf.5" "${STAGEDIR}/usr/share/man/man5/"
cp "${REPO_ROOT}/man/man5/mail_filter_rules.conf.5" "${STAGEDIR}/usr/share/man/man5/"
cp "${REPO_ROOT}/man/man5/mail_filter_whitelist.cidr.5" "${STAGEDIR}/usr/share/man/man5/"
cp "${REPO_ROOT}/man/man8/mail_filter.8" "${STAGEDIR}/usr/share/man/man8/"

# --- stage the Debian-specific pieces ---------------------------------------

# Debian Policy 12.5 requires a copyright file in every binary package, and
# 12.7 a changelog under the same directory. Both are static files kept in the
# repository rather than generated at build time, so that two builds of the
# same commit produce the same package.
cp "${SCRIPT_DIR}/copyright" "${STAGEDIR}/usr/share/doc/${PKG}/copyright"
gzip -9 -n -c "${SCRIPT_DIR}/changelog"     > "${STAGEDIR}/usr/share/doc/${PKG}/changelog.Debian.gz"

cp "${SCRIPT_DIR}/systemd/mail_filter.service" "${STAGEDIR}/usr/lib/systemd/system/mail_filter.service"
cp "${SCRIPT_DIR}/logrotate.d/mail_filter" "${STAGEDIR}/etc/logrotate.d/mail_filter"
install -m 0755 "${SCRIPT_DIR}/postinst" "${STAGEDIR}/DEBIAN/postinst"
install -m 0755 "${SCRIPT_DIR}/prerm" "${STAGEDIR}/DEBIAN/prerm"
install -m 0755 "${SCRIPT_DIR}/postrm" "${STAGEDIR}/DEBIAN/postrm"

# --- __pycache__ never ships -------------------------------------------------

find "${STAGEDIR}" -name '__pycache__' -type d -prune -exec rm -rf {} +

# --- permissions -------------------------------------------------------------

echo '--- setting permissions ---'
find "${STAGEDIR}" -type d -exec chmod 0755 {} +
chmod 0755 "${STAGEDIR}/usr/sbin/mail_filter"
chmod 0755 "${STAGEDIR}/usr/lib/mail_filter/mail_filter.py"
find "${STAGEDIR}/usr/lib/mail_filter/mail_filter" -name '*.py' -exec chmod 0644 {} +
chmod 0555 "${STAGEDIR}/usr/lib/mail_filter/generate_emoji_regex.py"
chmod 0555 "${STAGEDIR}/usr/lib/mail_filter/check_regex.py"
chmod 0644 "${STAGEDIR}/usr/lib/systemd/system/mail_filter.service"
chmod 0640 "${STAGEDIR}"/etc/mail_filter/*.dpkg-dist
chmod 0644 "${STAGEDIR}/etc/logrotate.d/mail_filter"
chmod 0644 "${STAGEDIR}/usr/share/doc/${PKG}/copyright"
chmod 0644 "${STAGEDIR}/usr/share/doc/${PKG}/changelog.Debian.gz"

for _mp in \
    "${STAGEDIR}/usr/share/man/man1/generate_emoji_regex.py.1" \
    "${STAGEDIR}/usr/share/man/man1/check_regex.py.1" \
    "${STAGEDIR}/usr/share/man/man8/mail_filter.8" \
    "${STAGEDIR}/usr/share/man/man5/mail_filter.conf.5" \
    "${STAGEDIR}/usr/share/man/man5/mail_filter_rules.conf.5" \
    "${STAGEDIR}/usr/share/man/man5/mail_filter_whitelist.cidr.5"
do
    gzip -9 -n -c "${_mp}" > "${_mp}.gz"
    rm -f "${_mp}"
    chmod 0644 "${_mp}.gz"
done

# --- DEBIAN/control, generated from the template ----------------------------

INSTALLED_SIZE=$(find "${STAGEDIR}" -not -path "${STAGEDIR}/DEBIAN/*" \
    -type f | xargs du -cb 2>/dev/null | tail -1 | awk '{print int($1/1024)+1}')

sed -e "s/@VERSION@/${VERSION}/" -e "s/@INSTALLED_SIZE@/${INSTALLED_SIZE}/" \
    "${SCRIPT_DIR}/control.template" > "${STAGEDIR}/DEBIAN/control"

echo
echo '--- DEBIAN/control ---'
cat "${STAGEDIR}/DEBIAN/control"

# --- DEBIAN/md5sums ---------------------------------------------------------
#
# Not required by policy, but it is what dpkg --verify and debsums(1)
# read; without it neither can tell whether an installed file has been
# altered since the package put it there.

( cd "${STAGEDIR}" \
  && find . -type f -not -path './DEBIAN/*' -printf '%P\0' \
  | sort -z | xargs -0 md5sum > DEBIAN/md5sums )
chmod 0644 "${STAGEDIR}/DEBIAN/md5sums"

echo
echo '--- building deb ---'
# --root-owner-group is not cosmetic: dpkg-deb otherwise records the build
# user as the owner of every file, and the package then installs the daemon
# code owned by whoever happened to run this script.
dpkg-deb --root-owner-group --build "${STAGEDIR}" "${DEBFILE}"

echo
echo "  Package : ${DEBFILE}"
echo "  Size    : $(du -sh "${DEBFILE}" | cut -f1)"
echo
echo "  Install : apt install ${DEBFILE}"
echo "  Remove  : apt-get remove ${PKG}"
echo "  Purge   : apt-get purge ${PKG}"
echo "  Info    : dpkg -s ${PKG}"
echo
echo "  NOTE: Depends on python3-milter (Ubuntu universe)."
echo "        If installation fails with unmet dependencies, enable universe first:"
echo "          add-apt-repository universe && apt-get update"
