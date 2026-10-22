# Camera audio

Audio capture and listening are disabled for every camera by default. The application probes each camera's RTSP stream when it is added; existing cameras report audio support as unknown until an administrator runs **Probe camera audio** on the camera card.

An administrator can enable **audio recording and operator listening** only after a successful probe reports an audio track. Operators can then listen from the live-view tile and hear the audio track in authenticated MP4 playback. Operators cannot change the camera's audio setting. Disabling audio takes effect after the recording worker restarts; existing footage is not rewritten.

Continuous and motion recordings include an optional audio track only while the camera's audio setting is enabled. FFmpeg normalizes captured audio to AAC in MP4 so browser playback has a broadly supported format. Live listening is a separately authenticated audio-only stream normalized to mono MP3. The server bounds live viewing at 16 concurrent video slots and 16 concurrent audio-listener slots; they are separate limits. Listening stops when the browser closes the media element or session. These admission limits do not certify host capacity for all slots under every camera workload.

Capability can change if a camera profile changes. The administrator probe updates the reported state and automatically disables audio if the current stream no longer has an audio track. Re-test after changing a camera's selected profile. The first probe describes the configured main RTSP profile; the optional lower-resolution grid profile is used for video viewing and is not used for audio.

AAC/MP4 and MP3 support is common in current browsers, but device, browser, proxy, and codec policies vary. Test live listening and playback on the intended clients. The probe confirms an audio track exists; it does not certify sound quality or prove that recorded audio was intelligible. Treat audio footage as sensitive workplace data and apply the same access, retention, preservation, backup, and deletion controls as video footage.
