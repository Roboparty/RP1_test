import type { TargetPartCode } from "./types";

export type PartModelAsset = {
  poster: string;
};

const modelVersion = "20260921-battery-product-white";

export const partModelAssets: Partial<Record<TargetPartCode, PartModelAsset>> = {
  SYS: {
    poster: "/assets/humanoid-robot.webp"
  },
  UPPER: {
    poster: "/model-posters/upper-product-white.webp"
  },
  LOWER: {
    poster: "/model-posters/lower-product-white.webp"
  },
  CHEST: {
    poster: "/model-posters/torso-product-white.webp"
  },
  HEAD: {
    poster: "/model-posters/head-product-white.webp"
  },
  BAT: {
    poster: `/model-posters/bat.webp?v=${modelVersion}`
  },
  ARM: {
    poster: "/model-posters/single-arm-product-white.webp"
  },
  SARM: {
    poster: "/model-posters/single-arm-product-white.webp"
  },
  LEG: {
    poster: "/model-posters/single-leg-product-white.webp"
  },
  SLEG: {
    poster: "/model-posters/single-leg-product-white.webp"
  }
};

export function getPartModelAsset(partCode: TargetPartCode) {
  return partModelAssets[partCode] ?? null;
}
