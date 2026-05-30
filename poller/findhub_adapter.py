"""Thin adapter over GoogleFindMyTools.

This is the **only** file in TagTrail that imports GoogleFindMyTools (GFMT).
Everything else in the poller depends on this module's API. If GFMT changes its
internals, we change this one file.

Public API:

    get_locations(tracker_ids: list[str], timeout_s: int) -> list[Fix]

Behaviour:

- Sends a "locate tracker" request for each ID over Google's Find Hub /
  Nova action API.
- The responses come asynchronously over FCM. We wait up to `timeout_s` total
  for them to arrive, decrypt as they arrive, and return whatever we have when
  the timeout fires. Missing responses are normal; return what you have.

We deliberately do NOT reimplement any GFMT crypto. We orchestrate calls to
GFMT primitives:
    - `create_location_request(...)` builds the encrypted Nova request payload.
    - `nova_request(...)` ships it.
    - `FcmReceiver.register_for_location_updates(cb)` registers our FCM token
      and gives us decoded-bytes callbacks for every inbound payload.
    - `parse_device_update_protobuf(hex)` decodes the FCM payload.
    - `retrieve_identity_key`, `decrypt`, `decrypt_aes_gcm` are GFMT crypto
      primitives we call as-is to turn encrypted reports into lat/lng.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .chrome_compat import apply_chromedriver_compat_patch
from .gfmt_secrets import GfmtSecretsError, materialize_if_missing
from .install_gfmt import GFMT_PINNED_COMMIT, gfmt_path
from .models import Fix

logger = logging.getLogger(__name__)


_CI_PATCHED = False


def _patch_shared_key_retrieval_for_ci() -> None:
    """Prevent GFMT's SharedKeyRetrieval from blocking on input() in CI.

    If the shared_key/owner_key aren't in secrets.json, GFMT prompts the user
    to press Enter then opens Chrome — impossible in a headless CI runner.
    We monkey-patch `_retrieve_shared_key` to raise immediately instead of
    hanging on stdin.
    """
    global _CI_PATCHED
    if _CI_PATCHED:
        return
    _CI_PATCHED = True

    if sys.stdin is not None and sys.stdin.isatty():
        return

    try:
        from KeyBackup import shared_key_retrieval

        def _no_interactive_retrieval():
            raise FindHubError(
                "GFMT needs to re-fetch E2E encryption keys (shared_key/owner_key) "
                "but this requires an interactive Chrome session. Re-run "
                "`tagtrail-gfmt-auth` on your machine, then update the "
                "GFMT_SECRETS_JSON_B64 secret in your GitHub fork."
            )

        shared_key_retrieval._retrieve_shared_key = _no_interactive_retrieval
        logger.debug("Patched SharedKeyRetrieval to fail-fast in non-interactive mode.")
    except ImportError:
        pass


def _ensure_gfmt_on_path() -> None:
    """Put the GFMT checkout on sys.path. Safe to call repeatedly.

    Also materializes ``Auth/secrets.json`` from $GFMT_SECRETS_JSON_B64 if the
    file is missing (fresh container / first-run cloud deploy). Doing this
    *before* sys.path is manipulated would be premature — GFMT needs to be
    cloned first — but it must happen before any GFMT module that consumes the
    file is imported. Inserting it here, between gfmt_path() resolution and
    the FCM/Nova imports in `_import_gfmt`, is the right seam.
    """
    p = gfmt_path()
    if p is None:
        raise FindHubError(
            "GoogleFindMyTools checkout not found. Run `tagtrail-install-gfmt` "
            "(or set GFMT_DIR=/path/to/GoogleFindMyTools). "
            f"Expected pinned commit: {GFMT_PINNED_COMMIT}."
        )
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
    try:
        materialize_if_missing()
    except GfmtSecretsError as e:
        raise FindHubError(str(e)) from e
    apply_chromedriver_compat_patch()
    _patch_shared_key_retrieval_for_ci()


def _import_gfmt():
    """Import GoogleFindMyTools lazily so unit tests can run without it installed."""

    _ensure_gfmt_on_path()

    # NovaApi: request builders + transport
    from NovaApi.ExecuteAction.LocateTracker.location_request import create_location_request
    from NovaApi.ListDevices.nbe_list_devices import request_device_list
    from NovaApi.nova_request import nova_request
    from NovaApi.scopes import NOVA_ACTION_API_SCOPE
    from NovaApi.util import generate_random_uuid

    # ProtoDecoders: parse the FCM payload + the device-list payload
    from ProtoDecoders import Common_pb2, DeviceUpdate_pb2
    from ProtoDecoders.decoder import (
        get_canonic_ids,
        parse_device_list_protobuf,
        parse_device_update_protobuf,
    )

    # Auth: the FCM receiver that delivers async responses
    from Auth.fcm_receiver import FcmReceiver

    # Crypto: identity key retrieval + decryption (we do not reimplement these)
    from NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
        is_mcu_tracker,
        retrieve_identity_key,
    )
    from FMDNCrypto.foreign_tracker_cryptor import decrypt as foreign_decrypt
    from KeyBackup.cloud_key_decryptor import decrypt_aes_gcm

    return {
        "create_location_request": create_location_request,
        "nova_request": nova_request,
        "NOVA_ACTION_API_SCOPE": NOVA_ACTION_API_SCOPE,
        "generate_random_uuid": generate_random_uuid,
        "Common_pb2": Common_pb2,
        "DeviceUpdate_pb2": DeviceUpdate_pb2,
        "parse_device_update_protobuf": parse_device_update_protobuf,
        "FcmReceiver": FcmReceiver,
        "is_mcu_tracker": is_mcu_tracker,
        "retrieve_identity_key": retrieve_identity_key,
        "foreign_decrypt": foreign_decrypt,
        "decrypt_aes_gcm": decrypt_aes_gcm,
        "request_device_list": request_device_list,
        "parse_device_list_protobuf": parse_device_list_protobuf,
        "get_canonic_ids": get_canonic_ids,
    }


@dataclass(frozen=True)
class AvailableTracker:
    """A tracker the GFMT account can see, regardless of whether we poll it."""

    id: str
    name: str


def list_available_trackers() -> list[AvailableTracker]:
    """Ask Find Hub which trackers this account owns/sees.

    This is a cheap HTTP call (no Chrome, no FCM), so it's safe to call every
    poll cycle. We use it to keep ``TagTrail/config.json`` current so the SPA
    picker reflects the user's actual inventory.

    Returns an empty list on transient failure rather than raising; the caller
    should keep using the previous list in that case (the previous selection
    is the truth, and a temporary "could not list" should not zero it out).
    """
    try:
        gfmt = _import_gfmt()
    except Exception as e:  # noqa: BLE001
        raise FindHubError(
            "GoogleFindMyTools is not installed or its API has changed. "
            f"Underlying error: {e}"
        ) from e

    try:
        hex_payload = gfmt["request_device_list"]()
    except Exception as e:  # noqa: BLE001
        logger.warning("request_device_list failed: %s", e)
        return []
    if not hex_payload:
        logger.warning("request_device_list returned empty payload.")
        return []

    try:
        device_list = gfmt["parse_device_list_protobuf"](hex_payload)
        pairs = gfmt["get_canonic_ids"](device_list)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to parse device list: %s", e)
        return []

    # Filter to SPOT devices only (tracker tags). Skip phones (IDENTIFIER_ANDROID)
    # and other non-tag devices — TagTrail is for tracker tags only.
    try:
        DeviceUpdate_pb2 = __import__(
            "ProtoDecoders.DeviceUpdate_pb2", fromlist=["DeviceUpdate_pb2"]
        )
        IDENTIFIER_SPOT = DeviceUpdate_pb2.IDENTIFIER_SPOT
    except (ImportError, AttributeError):
        IDENTIFIER_SPOT = None

    out: list[AvailableTracker] = []
    for device in device_list.deviceMetadata:
        # Only include SPOT (tracker tag) devices
        if IDENTIFIER_SPOT is not None:
            try:
                if device.identifierInformation.type != IDENTIFIER_SPOT:
                    logger.debug(
                        "Skipping non-tag device: %s (type=%s)",
                        device.userDefinedDeviceName,
                        device.identifierInformation.type,
                    )
                    continue
            except AttributeError:
                pass

        id_info = device.identifierInformation
        canonic_ids = id_info.canonicIds.canonicId
        name = device.userDefinedDeviceName
        for c in canonic_ids:
            cid = c.id
            if not isinstance(cid, str) or not cid:
                continue
            out.append(AvailableTracker(id=cid, name=str(name) if name else cid[:8]))
    return out


class FindHubError(RuntimeError):
    """Raised when the GoogleFindMyTools dependency is unavailable or misbehaves.

    The bootstrap-revoked case (Google killed the credential) surfaces as this
    exception with a message suggesting the user re-run bootstrap. The poller's
    error handler differentiates and tells the user.
    """


def _send_with_retry(fn, *args, attempts: int = 3, base_delay: float = 1.5):
    """Call `fn(*args)`; retry with exponential backoff if it returns None
    or raises a transient exception. Google's Nova API throws 5xx occasionally,
    especially right after the tracker's state changes upstream.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            result = fn(*args)
            if result is not None:
                return result
        except Exception as e:  # noqa: BLE001
            last_exc = e
        if i < attempts - 1:
            delay = base_delay * (2**i)
            logger.info("Nova request transient failure, retrying in %.1fs...", delay)
            time.sleep(delay)
    if last_exc is not None:
        logger.warning("Nova request gave up after %d attempts: %s", attempts, last_exc)
    return None


def _fixes_from_device_update(
    device_update,
    canonic_id: str,
    gfmt: dict,
) -> list[Fix]:
    """Decrypt a single device-update proto into a list of `Fix`.

    Mirrors GFMT's `decrypt_location_response_locations` but returns structured
    data instead of printing. We rely on GFMT's crypto primitives.
    """
    Common_pb2 = gfmt["Common_pb2"]
    DeviceUpdate_pb2 = gfmt["DeviceUpdate_pb2"]
    retrieve_identity_key = gfmt["retrieve_identity_key"]
    is_mcu_tracker = gfmt["is_mcu_tracker"]
    foreign_decrypt = gfmt["foreign_decrypt"]
    decrypt_aes_gcm = gfmt["decrypt_aes_gcm"]

    device_registration = (
        device_update.deviceMetadata.information.deviceRegistration
    )
    identity_key = retrieve_identity_key(device_registration)
    is_mcu = is_mcu_tracker(device_registration)

    locations_proto = (
        device_update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    )

    network_locations = list(locations_proto.networkLocations)
    network_locations_time = list(locations_proto.networkLocationTimestamps)
    if locations_proto.HasField("recentLocation"):
        network_locations.append(locations_proto.recentLocation)
        network_locations_time.append(locations_proto.recentLocationTimestamp)

    # Diagnostic counters, so a "0 fixes" outcome is debuggable.
    counts = {
        "raw": len(network_locations),
        "semantic": 0,
        "decrypt_fail": 0,
        "out_of_range": 0,
        "zero_zero": 0,
        "kept": 0,
    }

    fixes: list[Fix] = []
    for loc, ts in zip(network_locations, network_locations_time):
        if loc.status == Common_pb2.Status.SEMANTIC:
            # Semantic ("Home", "Work") reports have no coordinates we can map. Skip.
            counts["semantic"] += 1
            continue

        encrypted_location = loc.geoLocation.encryptedReport.encryptedLocation
        public_key_random = loc.geoLocation.encryptedReport.publicKeyRandom

        try:
            if public_key_random == b"":
                # Own-report path: AES-GCM with sha256(identity_key)
                key = hashlib.sha256(identity_key).digest()
                decrypted_location = decrypt_aes_gcm(key, encrypted_location)
            else:
                time_offset = 0 if is_mcu else loc.geoLocation.deviceTimeOffset
                decrypted_location = foreign_decrypt(
                    identity_key, encrypted_location, public_key_random, time_offset
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to decrypt one location report for %s: %s", canonic_id, e)
            counts["decrypt_fail"] += 1
            continue

        proto_loc = DeviceUpdate_pb2.Location()
        proto_loc.ParseFromString(decrypted_location)

        lat = proto_loc.latitude / 1e7
        lng = proto_loc.longitude / 1e7

        # Sanity check: GFMT can produce zero/invalid points if decryption silently fails.
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            counts["out_of_range"] += 1
            continue
        if lat == 0.0 and lng == 0.0:
            counts["zero_zero"] += 1
            continue

        acc_val = float(loc.geoLocation.accuracy) if loc.geoLocation.accuracy else None

        fixes.append(
            Fix(
                id=canonic_id,
                ts=datetime.fromtimestamp(int(ts.seconds), tz=timezone.utc),
                lat=lat,
                lng=lng,
                acc=acc_val,
                src="network",
            )
        )
        counts["kept"] += 1

    if counts["raw"] == 0:
        logger.info(
            "FCM response for %s contained no location reports yet. The Find Hub "
            "network needs Android phones to have walked past the tracker recently. "
            "Try again in 10-30 minutes.",
            canonic_id,
        )
    elif counts["kept"] == 0:
        logger.warning(
            "FCM response for %s had %d report(s) but none were usable: "
            "%d semantic, %d decrypt-failed, %d out-of-range, %d (0,0). "
            "If decrypt-failed is high, owner key may need re-recovery.",
            canonic_id,
            counts["raw"],
            counts["semantic"],
            counts["decrypt_fail"],
            counts["out_of_range"],
            counts["zero_zero"],
        )
    else:
        logger.info(
            "Kept %d/%d fix(es) for %s (skipped: %d semantic, %d decrypt-fail, %d out-of-range, %d zero).",
            counts["kept"],
            counts["raw"],
            canonic_id,
            counts["semantic"],
            counts["decrypt_fail"],
            counts["out_of_range"],
            counts["zero_zero"],
        )

    return fixes


def get_locations(tracker_ids: list[str], timeout_s: int = 90) -> list[Fix]:
    """Request and decrypt the latest fixes for each tracker ID.

    Blocks up to `timeout_s` seconds total for asynchronous FCM responses.
    Returns whatever fixes arrived in that window — missing responses are
    normal and not an error.
    """
    if not tracker_ids:
        return []

    try:
        gfmt = _import_gfmt()
    except Exception as e:  # noqa: BLE001
        raise FindHubError(
            "GoogleFindMyTools is not installed or its API has changed. "
            "Reinstall the pinned version from pyproject.toml. "
            f"Underlying error: {e}"
        ) from e

    parse = gfmt["parse_device_update_protobuf"]
    create_location_request = gfmt["create_location_request"]
    nova_request = gfmt["nova_request"]
    NOVA_ACTION_API_SCOPE = gfmt["NOVA_ACTION_API_SCOPE"]
    generate_random_uuid = gfmt["generate_random_uuid"]
    FcmReceiver = gfmt["FcmReceiver"]

    # request_uuid -> canonical id, so we can route responses back to the requested tracker.
    pending: dict[str, str] = {}
    fixes: list[Fix] = []
    fixes_lock = threading.Lock()

    def on_payload(hex_string: str) -> None:
        try:
            update = parse(hex_string)
            req_uuid = update.fcmMetadata.requestUuid
            canonic_id = pending.get(req_uuid)
            if canonic_id is None:
                # Not one of our requests (could be a stray); ignore.
                return
            new_fixes = _fixes_from_device_update(update, canonic_id, gfmt)
            with fixes_lock:
                fixes.extend(new_fixes)
            # Mark this request as completed so we can short-circuit the wait once all done.
            pending.pop(req_uuid, None)
        except Exception as e:  # noqa: BLE001
            logger.warning("Error handling FCM payload: %s", e)

    try:
        receiver = FcmReceiver()
        fcm_token = receiver.register_for_location_updates(on_payload)
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "token" in msg and ("revoked" in msg or "invalid" in msg or "expired" in msg):
            raise FindHubError(
                "Find Hub credentials appear to be revoked or expired. "
                "Re-run `tagtrail-bootstrap` locally and redeploy the secrets."
            ) from e
        raise FindHubError(f"Could not register FCM receiver: {e}") from e

    failed_sends: list[str] = []
    for canonic_id in tracker_ids:
        uuid = generate_random_uuid()
        try:
            pending[uuid] = canonic_id
            hex_payload = create_location_request(canonic_id, fcm_token, uuid)
            # nova_request returns None on non-200 and prints to stdout. We treat
            # that as a send failure so we don't waste the full timeout waiting
            # for an FCM response that will never come.
            send_result = _send_with_retry(
                nova_request, NOVA_ACTION_API_SCOPE, hex_payload, attempts=3
            )
            if send_result is None:
                logger.warning(
                    "Nova API rejected the location request for %s (see [NovaRequest] log above). "
                    "Tracker will be skipped this cycle.",
                    canonic_id,
                )
                pending.pop(uuid, None)
                failed_sends.append(canonic_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to send location request for %s: %s", canonic_id, e)
            pending.pop(uuid, None)
            failed_sends.append(canonic_id)

    if not pending:
        if failed_sends:
            logger.error(
                "All %d send(s) failed (Nova API). Common causes: transient Google 5xx "
                "(retry in a minute), or the tracker is not actually shared with the "
                "signed-in account.",
                len(failed_sends),
            )
        with fixes_lock:
            return list(fixes)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and pending:
        time.sleep(0.5)

    if pending:
        logger.info(
            "Timed out waiting for %d of %d trackers (no FCM reply within %ds): %s",
            len(pending),
            len(tracker_ids),
            timeout_s,
            list(pending.values()),
        )

    with fixes_lock:
        return list(fixes)
