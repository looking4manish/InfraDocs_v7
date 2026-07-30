// Shared framer-motion variants for the Home lenses. Extracted from LensHome so
// each lens file can pull the same spring + stagger without duplicating them.

export const SPRING = { type: "spring", stiffness: 400, damping: 36 };

// Stagger parent + child variants. reduce=true collapses to instant.
export function gridVariants(reduce) {
  return {
    hidden: {},
    show: { transition: { staggerChildren: reduce ? 0 : 0.03 } },
  };
}

export function cardVariants(reduce) {
  return reduce
    ? { hidden: { opacity: 1, y: 0 }, show: { opacity: 1, y: 0 } }
    : { hidden: { opacity: 0, y: 8 }, show: { opacity: 1, y: 0, transition: SPRING } };
}
