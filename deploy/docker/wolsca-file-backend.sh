#!/usr/bin/env bash
#
# CUPS backend 'wolscafile' - the virtual printer of the test container.
#
# It writes the job data unchanged into the directory from the device URI:
#
#     lpadmin -p WolsCA_Output -v wolscafile:/var/spool/wolsca/PrintOut -E
#
# cups-pdf is deliberately not used for this: cups-pdf would render the job
# again and drop the result in its own (watched) folder, which sends the job
# straight back into the intake queue. This backend only copies, so the queue is
# raw and the PDF the service produced arrives byte for byte.
#
# CUPS calls a backend either without arguments (device discovery) or as
#   backend job-id user title copies options [file]
# With a file the data is in $6, without one it comes in on stdin.
#
set -u

if [ "$#" -eq 0 ]; then
    echo 'file wolscafile "Unknown" "Wols CA virtual file printer"'
    exit 0
fi

TARGET_DIR=${DEVICE_URI#wolscafile:}
TARGET_DIR=${TARGET_DIR#//}
[ -n "${TARGET_DIR}" ] || TARGET_DIR=/var/spool/wolsca/PrintOut

if ! mkdir -p "${TARGET_DIR}" 2>/dev/null; then
    echo "ERROR: cannot create ${TARGET_DIR}" >&2
    exit 1
fi

# Keep the job title recognisable, but never let it escape the directory.
SAFE_TITLE=$(printf '%s' "${3:-job}" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-60)
OUTPUT="${TARGET_DIR}/${SAFE_TITLE}-job${1:-0}-$(date +%Y%m%d%H%M%S)-$$.pdf"

if [ "$#" -ge 6 ] && [ -n "${6:-}" ]; then
    cp "$6" "${OUTPUT}" || exit 1
else
    cat > "${OUTPUT}" || exit 1
fi

chmod 0664 "${OUTPUT}" 2>/dev/null || true
echo "INFO: Wols CA virtual printer wrote ${OUTPUT}" >&2
exit 0
