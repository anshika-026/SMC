import { memo } from "react";

// The DECO Vision bot as a soft watermark behind every page: large, blurred,
// ~22% opacity, bottom-right, with a faint DeWIN blue/purple glow. It is one
// fixed layer behind all content (the layout isolates a stacking context and
// this sits at a negative z-index), never takes clicks or scroll, and only
// this layer is blurred / faded - the pages above it are untouched.
// The image is public/deco-bot.png; BASE_URL keeps the path right if the app
// is ever served under a sub-path.

const BOT_SRC = `${import.meta.env.BASE_URL}deco-bot.png`;

// Feathered edges so the picture's own background melts into the page.
const FEATHER = "radial-gradient(ellipse 50% 50% at 50% 50%, #000 45%, transparent 75%)";

function GlobalBackground() {
  return (
    <div aria-hidden="true" className="fixed inset-0 -z-10 pointer-events-none select-none overflow-hidden">
      {/* ambient glow */}
      <div
        className="absolute rounded-full"
        style={{
          right: "-12vmax",
          bottom: "-14vmax",
          width: "62vmax",
          height: "62vmax",
          background: "radial-gradient(circle at 45% 45%, rgba(79,70,229,0.10), rgba(37,99,235,0.06) 40%, transparent 70%)",
          filter: "blur(40px)",
        }}
      />
      {/* the bot */}
      <img
        src={BOT_SRC}
        alt=""
        draggable={false}
        decoding="async"
        className="absolute max-w-none"
        style={{
          right: "-3vmin",
          bottom: "-6vh",
          height: "min(92vh, 980px)",
          width: "auto",
          opacity: 0.22,
          filter: "blur(3px) saturate(1.15)",
          WebkitMaskImage: FEATHER,
          maskImage: FEATHER,
        }}
      />
    </div>
  );
}

// Static: never re-renders on navigation.
export default memo(GlobalBackground);
