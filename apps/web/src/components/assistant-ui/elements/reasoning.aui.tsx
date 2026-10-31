"use client";

import { forwardRef, memo, useCallback, useRef } from "react";
import { useScrollLock, type ReasoningMessagePartComponent } from "@assistant-ui/react";
import { MarkdownText } from "@/components/assistant-ui/elements/markdown-text";
import {
  ANIMATION_DURATION,
  ReasoningRoot as ReasoningRootBase,
  ReasoningTrigger,
  ReasoningContent,
  ReasoningText,
  ReasoningFade,
  reasoningVariants,
  type ReasoningRootProps,
} from "./reasoning";

export type { ReasoningRootProps } from "./reasoning";

/** `ReasoningRoot` with the thread viewport scroll locked during disclosure animations. */
const ReasoningRoot = forwardRef<HTMLDivElement, Omit<ReasoningRootProps, "ref">>(
  function ReasoningRoot({ onAnimationStart, ...props }, ref) {
    const collapsibleRef = useRef<HTMLDivElement | null>(null);
    const lockScroll = useScrollLock(collapsibleRef, ANIMATION_DURATION);

    const handleAnimationStart = useCallback(() => {
      lockScroll();
      onAnimationStart?.();
    }, [lockScroll, onAnimationStart]);

    const composedRef = useCallback(
      (node: HTMLDivElement | null) => {
        collapsibleRef.current = node;
        if (typeof ref === "function") {
          const cleanup: unknown = ref(node);
          if (typeof cleanup === "function") {
            return () => {
              collapsibleRef.current = null;
              (cleanup as () => void)();
            };
          }
        } else if (ref) {
          (ref as { current: HTMLDivElement | null }).current = node;
        }
        return undefined;
      },
      [ref],
    );

    return (
      <ReasoningRootBase ref={composedRef} onAnimationStart={handleAnimationStart} {...props} />
    );
  },
);

const ReasoningImpl: ReasoningMessagePartComponent = () => <MarkdownText />;

const Reasoning = memo(ReasoningImpl) as unknown as ReasoningMessagePartComponent & {
  Root: typeof ReasoningRoot;
  Trigger: typeof ReasoningTrigger;
  Content: typeof ReasoningContent;
  Text: typeof ReasoningText;
  Fade: typeof ReasoningFade;
};

Reasoning.displayName = "Reasoning";
Reasoning.Root = ReasoningRoot;
Reasoning.Trigger = ReasoningTrigger;
Reasoning.Content = ReasoningContent;
Reasoning.Text = ReasoningText;
Reasoning.Fade = ReasoningFade;

export {
  Reasoning,
  ReasoningRoot,
  ReasoningTrigger,
  ReasoningContent,
  ReasoningText,
  ReasoningFade,
  reasoningVariants,
};
