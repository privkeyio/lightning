#!/bin/sh
# If an argument is specified, that dir is checked before downloading,
# and updated after successful install.

set -e

export BITCOIN_VERSION=27.1
export ELEMENTS_VERSION=23.2.1

DIRNAME="bitcoin-${BITCOIN_VERSION}"
EDIRNAME="elements-${ELEMENTS_VERSION}"
FILENAME="${DIRNAME}-x86_64-linux-gnu.tar.gz"
EFILENAME="${EDIRNAME}-x86_64-linux-gnu.tar.gz"

cd /tmp/

# Since we inadvertently broke `elementsd` support in the past we only
# want to download and enable the daemon that is actually going to be
# used when running in CI. Otherwise we could end up accidentally
# testing against `bitcoind` but still believe that we ran against
# `elementsd`.
if [ "$TEST_NETWORK" = "liquid-regtest" ]; then
    if [ -f "$1/${EFILENAME}" ]; then
	cp "$1/${EFILENAME}" .
    else
	wget "https://github.com/ElementsProject/elements/releases/download/elements-${ELEMENTS_VERSION}/${EFILENAME}"
    fi
    tar -xf "${EFILENAME}"
    [ "$1" = "" ] || cp "${EFILENAME}" "$1"/
    sudo mv "${EDIRNAME}"/bin/* "/usr/local/bin"
    rm -rf "${EFILENAME}" "${EDIRNAME}"
elif [ "${BLAKE2B_CI:-0}" = "1" ]; then
    # Exercise unified signatures on a backend that enforces Blake2b consensus.
    KNOTS_VERSION=29.4.3.knots20260508
    KNOTS_FILE="bitcoin-${KNOTS_VERSION}-x86_64-linux-gnu.tar.gz"
    if [ -f "$1/${KNOTS_FILE}" ]; then
        cp "$1/${KNOTS_FILE}" .
    else
        wget "https://github.com/bitcoinknots/bitcoin/releases/download/v${KNOTS_VERSION}/${KNOTS_FILE}"
    fi
    echo "4c67f1139b668fc3b98e339dd16112c636b2c0f42c2294b98fb92d1d27943e24  ${KNOTS_FILE}" | sha256sum -c -
    tar -xf "${KNOTS_FILE}"
    [ "$1" = "" ] || cp "${KNOTS_FILE}" "$1"/
    sudo install -D -m 755 "bitcoin-${KNOTS_VERSION}/bin/bitcoind" /usr/local/libexec/paperclip-bitcoind
    sudo install -m 755 "bitcoin-${KNOTS_VERSION}/bin/bitcoin-cli" /usr/local/bin/bitcoin-cli
    # CLN's randomized anti-fee-sniping locktime can legitimately equal 21.
    # Disable that overlay heuristic in tests, retaining consensus validation.
    cat > paperclip-bitcoind-wrapper <<'WRAPPER'
#!/bin/sh
exec /usr/local/libexec/paperclip-bitcoind -testactivationheight=blake2b@${BLAKE2B_ACTIVATION_HEIGHT:-1} -rejectparasites=0 "$@"
WRAPPER
    sudo install -m 755 paperclip-bitcoind-wrapper /usr/local/bin/bitcoind
else
    if [ -f "$1/${FILENAME}" ]; then
	cp "$1/${FILENAME}" .
    else
	wget "https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}/${FILENAME}"
    fi
    tar -xf "${FILENAME}"
    [ "$1" = "" ] || cp "${FILENAME}" "$1"/
    sudo mv "${DIRNAME}"/bin/* "/usr/local/bin"
    rm -rf "${FILENAME}" "${DIRNAME}"
fi
