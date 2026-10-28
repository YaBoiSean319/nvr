/** What this browser can play: H.265 changes where the NVR sends it as it is and where it converts it to H.264 first. */
export const HEVC_MIME = 'video/mp4; codecs="hvc1.1.6.L93.B0"';

const HEVC_NAMES = new Set(["hevc", "h265", "hev1", "hvc1"]);

export const isHevc = (codec: string | null | undefined): boolean => HEVC_NAMES.has((codec ?? "").toLowerCase());

/** Whether Media Source Extensions can play H.265 here (live video). */
export function mseHevc(): boolean {
  try {
    return typeof MediaSource !== "undefined" && MediaSource.isTypeSupported(HEVC_MIME);
  } catch {
    return false;
  }
}

/** Whether a <video> element can play an H.265 file here (recorded video). */
export function fileHevc(): boolean {
  try {
    return typeof document !== "undefined" && document.createElement("video").canPlayType(HEVC_MIME) !== "";
  } catch {
    return false;
  }
}

/** A recording's address, asking for H.264 where the camera records H.265 and this browser cannot play it. */
export function playableUrl(url: string, codec: string | null | undefined): string {
  return isHevc(codec) && !fileHevc() ? `${url}${url.includes("?") ? "&" : "?"}compat=h264` : url;
}
