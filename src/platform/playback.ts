export interface Playback {
  focused: boolean;
  media: {
    time: number;
    duration: number;
    paused: boolean;
    ended: boolean;
    readyState: number;
    error: boolean;
  } | null;
}

export interface LearningProgress {
  description: string;
  completed: number;
  total: number | null;
  state: "playing" | "paused" | "buffering" | "syncing" | "reading";
}

/** Read-only sampling; it never starts playback or advances platform progress. */
export function playbackSnapshot(selector: string): Playback {
  const focused = !!document
    .querySelector(selector)
    ?.classList.contains("focus");
  const video = focused
    ? [...document.querySelectorAll("video")].find(
        (element) => element.getBoundingClientRect().height > 0,
      )
    : undefined;
  return {
    focused,
    media: video
      ? {
          time: video.currentTime,
          duration: video.duration,
          paused: video.paused,
          ended: video.ended,
          readyState: video.readyState,
          error: !!video.error,
        }
      : null,
  };
}

export function learningProgress(
  description: string,
  video: boolean,
  elapsed: number,
  media: Playback["media"],
): LearningProgress {
  if (!video)
    return {
      description,
      completed: Math.floor(elapsed),
      total: null,
      state: "reading",
    };
  return {
    description,
    completed: Math.floor(media?.time ?? 0),
    total:
      media && Number.isFinite(media.duration) && media.duration > 0
        ? Math.ceil(media.duration)
        : null,
    state: !media
      ? "buffering"
      : media.ended
        ? "syncing"
        : media.readyState < 3
          ? "buffering"
          : media.paused
            ? "paused"
            : "playing",
  };
}
