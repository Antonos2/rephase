import React from "react";
import { Composition } from "remotion";
import { RephaseVideo } from "./RephaseVideo";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      {/* TikTok / Reels — 1080×1920 */}
      <Composition
        id="RephaseVideoVertical"
        component={RephaseVideo}
        durationInFrames={1800}
        fps={30}
        width={1080}
        height={1920}
      />
      {/* YouTube — 1920×1080 */}
      <Composition
        id="RephaseVideoHorizontal"
        component={RephaseVideo}
        durationInFrames={1800}
        fps={30}
        width={1920}
        height={1080}
      />
    </>
  );
};
