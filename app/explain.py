"""Plain-English explanations of Jellyfin transcode reasons, with something the viewer can do about it."""
from __future__ import annotations

from collections import Counter

# reason code -> (group key, short explanation, what you can do)
REASONS: dict[str, tuple[str, str, str]] = {
    "VideoCodecNotSupported": ("video", "Your device can’t play this video format{vc}.",
                               "Try the Jellyfin app instead of a web browser, or watch on another device."),
    "VideoProfileNotSupported": ("video", "Your device can’t play this video format{vc}.",
                                 "Try the Jellyfin app instead of a web browser, or watch on another device."),
    "VideoLevelNotSupported": ("video", "Your device can’t play this video format{vc}.",
                               "Try the Jellyfin app instead of a web browser, or watch on another device."),
    "VideoResolutionNotSupported": ("video", "Your device can’t play this resolution{res}.",
                                    "Watch on a TV or newer device for full quality."),
    "VideoBitDepthNotSupported": ("video", "Your device can’t play 10-bit video.",
                                  "Try the Jellyfin app or a newer device."),
    "VideoFramerateNotSupported": ("video", "Your device can’t play this frame rate.", "Try a different device."),
    "RefFramesNotSupported": ("video", "Your device can’t play this video directly.", "Try the Jellyfin app or a different device."),
    "AnamorphicVideoNotSupported": ("video", "Your device can’t play this video directly.", "Try the Jellyfin app or a different device."),
    "InterlacedVideoNotSupported": ("video", "Your device can’t play interlaced video.", "Try the Jellyfin app or a different device."),
    "VideoRangeTypeNotSupported": ("hdr", "Your screen doesn’t support HDR / Dolby Vision, so the picture is converted.",
                                   "That’s normal — colours are adjusted for your screen. An HDR TV plays it directly."),
    "AudioCodecNotSupported": ("audio", "Your device can’t play this audio track{ac}.",
                               "In the player, pick another audio track (AAC, AC3 or stereo) if there is one."),
    "AudioChannelsNotSupported": ("audio", "Your device can’t play surround sound on this track.",
                                  "Pick a stereo audio track in the player, if there is one."),
    "AudioProfileNotSupported": ("audio", "Your device can’t play this audio track{ac}.",
                                 "Pick another audio track in the player, if there is one."),
    "AudioSampleRateNotSupported": ("audio", "Your device can’t play this audio track{ac}.",
                                    "Pick another audio track in the player, if there is one."),
    "AudioBitDepthNotSupported": ("audio", "Your device can’t play this audio track{ac}.",
                                  "Pick another audio track in the player, if there is one."),
    "SecondaryAudioNotSupported": ("audio", "Your device can’t switch to this audio track directly.",
                                   "Use the default audio track to avoid converting."),
    "SubtitleCodecNotSupported": ("subs", "These subtitles have to be burned into the picture.",
                                  "Choose a different subtitle track (SRT / text) or turn subtitles off."),
    "ContainerNotSupported": ("container", "Your device can’t open this file type.",
                              "Try the Jellyfin app or a different device."),
    "ContainerBitrateExceedsLimit": ("bandwidth", "The quality is higher than your streaming limit allows.",
                                     "Lower the quality in the player’s settings, or ask the admin to raise your limit."),
    "VideoBitrateNotSupported": ("bandwidth", "The video quality is higher than your device or connection allows.",
                                 "Lower the quality in the player’s settings."),
    "AudioBitrateNotSupported": ("bandwidth", "The audio quality is higher than your device allows.",
                                 "Pick another audio track in the player."),
    "UnknownVideoStreamInfo": ("other", "Jellyfin couldn’t read this video’s details, so it converted it to be safe.", ""),
    "UnknownAudioStreamInfo": ("other", "Jellyfin couldn’t read this audio’s details, so it converted it to be safe.", ""),
    "DirectPlayError": ("other", "Playing the original file failed, so Jellyfin converted it.", "Try again, or use the Jellyfin app."),
}
GROUP_TITLE = {"video": "Video format", "hdr": "HDR", "audio": "Audio", "subs": "Subtitles",
               "container": "File type", "bandwidth": "Streaming limit", "other": "Other"}


def _human(code: str) -> str:
    out = ""
    for ch in code:
        out += (" " + ch.lower()) if ch.isupper() and out else ch
    return out.capitalize()


def explain(reasons: str | None, vc: str | None = None, ac: str | None = None, res: str | None = None) -> list[dict]:
    """'VideoCodecNotSupported, AudioCodecNotSupported' -> [{group, title, text, tip}], one per group."""
    seen, out = set(), []
    for code in [r.strip() for r in (reasons or "").split(",") if r.strip()]:
        group, text, tip = REASONS.get(code, ("other", _human(code) + ".", ""))
        if group in seen:
            continue
        seen.add(group)
        text = text.format(vc=f" ({vc.upper()})" if vc else "", ac=f" ({ac.upper()})" if ac else "",
                           res=f" ({res})" if res else "")
        out.append({"group": group, "title": GROUP_TITLE[group], "text": text, "tip": tip})
    # HDR conversion is harmless; show the actionable ones first
    out.sort(key=lambda x: x["group"] == "hdr")
    return out


def summary(plays) -> dict | None:
    """For a viewer's recent plays: how many transcoded, and the most common reason + tip."""
    plays = list(plays)
    if not plays:
        return None
    tc = [p for p in plays if p.play_method == "Transcode"]
    if not tc:
        return {"total": len(plays), "transcodes": 0}
    groups: Counter = Counter()
    clients: Counter = Counter()
    for p in tc:
        for e in explain(p.transcode_reasons):
            groups[e["group"]] += 1
        clients[p.client or "your device"] += 1
    top = groups.most_common(1)[0][0] if groups else None
    sample = next((e for p in tc for e in explain(p.transcode_reasons) if e["group"] == top), None) if top else None
    return {"total": len(plays), "transcodes": len(tc), "top": sample, "top_count": groups.get(top, 0),
            "client": clients.most_common(1)[0][0]}
