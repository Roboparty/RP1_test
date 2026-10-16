import { rm } from "node:fs/promises";

const dist = new URL("../dist/", import.meta.url);
const unshippedAssets = [
  "models",
  "assets/rp1-hero-back.webp",
  "assets/rp1-hero-blueprint.webp",
  "assets/rp1-hero-front.webp",
  "assets/rp1-hero-full.webp",
  "assets/rp1-hero-portrait.webp",
  "assets/rp1-hero-side.webp",
  "assets/humanoid-robot.png",
  "assets/rp1-waving-robot.png",
  "model-posters/bat.png",
  "model-posters/chest.png",
  "model-posters/head.png",
  "model-posters/head-product-white.png",
  "model-posters/lower.png",
  "model-posters/lower-product-white.png",
  "model-posters/sarm.png",
  "model-posters/single-arm-product-white.png",
  "model-posters/single-leg-product-white.png",
  "model-posters/sleg.png",
  "model-posters/sys.png",
  "model-posters/torso-product-white.png",
  "model-posters/upper.png",
  "model-posters/upper.webp"
];

await Promise.all(
  unshippedAssets.map((path) => rm(new URL(path, dist), { recursive: true, force: true }))
);
