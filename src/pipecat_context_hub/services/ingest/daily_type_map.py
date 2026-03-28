"""Static method-to-type mapping for daily-co/daily-python.

Maps CallClient methods and EventHandler callbacks to their RST type
definitions from ``docs/src/types.rst``. This mapping is used at ingestion
time to populate ``related_types`` metadata on ``.pyi`` method chunks,
enabling cross-referencing between method signatures and their dict schemas.

Source: https://reference-python.daily.co/api_reference.html

This is a static table rather than a heuristic because method names and
type names have low substring overlap (e.g. ``join`` → ``ClientSettings``,
``start_recording`` → ``StreamingSettings``).
"""

from __future__ import annotations

# CallClient method → RST type name(s)
CALL_CLIENT_METHOD_TYPES: dict[str, list[str]] = {
    "join": ["ClientSettings"],
    "send_dtmf": ["DialoutSendDtmfSettings"],
    "start_dialout": ["DialoutSettings"],
    "start_recording": ["StreamingSettings"],
    "start_transcription": ["TranscriptionSettings"],
    "start_live_stream_with_endpoints": ["StreamingSettings"],
    "start_live_stream_with_rtmp_urls": ["StreamingSettings"],
    "update_inputs": ["InputSettings"],
    "update_publishing": ["PublishingSettings"],
    "update_recording": ["StreamingUpdateSettings"],
    "update_live_stream": ["StreamingUpdateSettings"],
    "update_permissions": ["ParticipantPermissions"],
    "update_remote_participants": ["RemoteParticipantUpdates"],
    "update_subscription_profiles": ["SubscriptionProfileSettings"],
    "update_subscriptions": ["ParticipantSubscriptions"],
    "sip_call_transfer": ["SipCallTransferSettings"],
    "sip_refer": ["SipCallTransferSettings"],
    "set_ice_config": ["IceConfig"],
}

# EventHandler callback → RST type name(s)
EVENT_HANDLER_PARAM_TYPES: dict[str, list[str]] = {
    "on_active_speaker_changed": ["Participant"],
    "on_available_devices_updated": ["AvailableDevices"],
    "on_call_state_updated": ["CallState"],
    "on_dialin_connected": ["DialinConnectedEvent"],
    "on_dialin_error": ["DialinEvent"],
    "on_dialin_stopped": ["DialinStoppedEvent"],
    "on_dialin_warning": ["DialinEvent"],
    "on_dialout_answered": ["DialoutEvent"],
    "on_dialout_connected": ["DialoutEvent"],
    "on_dialout_error": ["DialoutEvent"],
    "on_dialout_stopped": ["DialoutEvent"],
    "on_dialout_warning": ["DialoutEvent"],
    "on_dtmf_event": ["DtmfEvent"],
    "on_inputs_updated": ["InputSettings"],
    "on_live_stream_started": ["LiveStreamStatus"],
    "on_live_stream_updated": ["LiveStreamUpdate"],
    "on_network_stats_updated": ["NetworkStats"],
    "on_participant_counts_updated": ["ParticipantCounts"],
    "on_participant_joined": ["Participant"],
    "on_participant_left": ["Participant", "ParticipantLeftReason"],
    "on_participant_updated": ["Participant"],
    "on_publishing_updated": ["PublishingSettings"],
    "on_recording_started": ["RecordingStatus"],
    "on_subscription_profiles_updated": ["SubscriptionProfileSettings"],
    "on_subscriptions_updated": ["ParticipantSubscriptions"],
    "on_transcription_message": ["TranscriptionMessage"],
    "on_transcription_started": ["TranscriptionStatus"],
    "on_transcription_updated": ["TranscriptionUpdated"],
}

# Combined lookup: method_name → type names (for any Daily class)
ALL_METHOD_TYPES: dict[str, list[str]] = {
    **CALL_CLIENT_METHOD_TYPES,
    **EVENT_HANDLER_PARAM_TYPES,
}


