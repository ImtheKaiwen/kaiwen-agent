from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree.ElementTree import Element, SubElement, tostring

from kaiwen_agent.telephony.base import OutboundPhoneCall, OutboundPhoneCallReceipt


class TwilioTelephonyError(RuntimeError):
    """Sanitized Twilio transport or response failure."""


class TwilioHTTPTransport(Protocol):
    async def post_form(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        form: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class UrllibTwilioHTTPTransport:
    async def post_form(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        form: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(
            self._post_form_sync,
            url,
            dict(headers),
            dict(form),
            timeout_seconds,
        )

    @staticmethod
    def _post_form_sync(
        url: str,
        headers: dict[str, str],
        form: dict[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        request_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
        request = Request(
            url,
            data=urlencode(form).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise TwilioTelephonyError(f"Twilio request failed with HTTP {exc.code}") from exc
        except (URLError, json.JSONDecodeError) as exc:
            raise TwilioTelephonyError("Twilio request failed") from exc
        if not isinstance(payload, Mapping):
            raise TwilioTelephonyError("Twilio returned an invalid response")
        return payload


class TwilioSIPOutboundService:
    """Originate a PSTN call and bridge it to a configured SIP destination."""

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        sip_target_uri: str,
        transport: TwilioHTTPTransport | None = None,
        base_url: str = "https://api.twilio.com/2010-04-01",
        timeout_seconds: float = 15,
    ) -> None:
        if not account_sid.startswith("AC"):
            raise ValueError("account_sid must be a Twilio Account SID")
        if not auth_token:
            raise ValueError("auth_token must not be empty")
        self._validate_e164(from_number, field="from_number")
        if not sip_target_uri.startswith("sip:"):
            raise ValueError("sip_target_uri must use sip:")
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_number = from_number
        self._sip_target_uri = sip_target_uri
        self._transport = transport or UrllibTwilioHTTPTransport()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def place_call(self, request: OutboundPhoneCall) -> OutboundPhoneCallReceipt:
        self._validate_e164(request.to_number, field="to_number")
        if request.status_callback_url and not request.status_callback_url.startswith("https://"):
            raise ValueError("status_callback_url must use https")
        sip_uri = self._sip_target_uri
        if request.sip_headers:
            for name, value in request.sip_headers.items():
                if not name.lower().startswith("x-") or not name.replace("-", "").isalnum():
                    raise ValueError("SIP header names must be safe x- headers")
                if not value or any(character in value for character in "\r\n"):
                    raise ValueError("SIP header values must not be empty or contain newlines")
            separator = "&" if "?" in sip_uri else "?"
            sip_uri = f"{sip_uri}{separator}{urlencode(request.sip_headers)}"

        response = Element("Response")
        dial = SubElement(response, "Dial")
        SubElement(dial, "Sip").text = sip_uri
        form = {
            "To": request.to_number,
            "From": self._from_number,
            "Twiml": tostring(response, encoding="unicode"),
        }
        if request.status_callback_url:
            form.update(
                {
                    "StatusCallback": request.status_callback_url,
                    "StatusCallbackMethod": "POST",
                    "StatusCallbackEvent": "initiated ringing answered completed",
                }
            )
        credentials = base64.b64encode(f"{self._account_sid}:{self._auth_token}".encode()).decode(
            "ascii"
        )
        payload = await self._transport.post_form(
            f"{self._base_url}/Accounts/{self._account_sid}/Calls.json",
            headers={"Authorization": f"Basic {credentials}"},
            form=form,
            timeout_seconds=self._timeout_seconds,
        )
        call_sid = payload.get("sid")
        status = payload.get("status", "queued")
        if not isinstance(call_sid, str) or not call_sid.startswith("CA"):
            raise TwilioTelephonyError("Twilio returned an invalid call identifier")
        if not isinstance(status, str):
            status = "queued"
        return OutboundPhoneCallReceipt(
            provider="twilio",
            provider_call_id=call_sid,
            status=status,
        )

    @staticmethod
    def _validate_e164(value: str, *, field: str) -> None:
        if not value.startswith("+") or not value[1:].isdigit() or not 8 <= len(value[1:]) <= 15:
            raise ValueError(f"{field} must be an E.164 phone number")


__all__ = [
    "TwilioHTTPTransport",
    "TwilioSIPOutboundService",
    "TwilioTelephonyError",
    "UrllibTwilioHTTPTransport",
]
