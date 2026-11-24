// Whether the transcript follows new content (auto-scroll only at the bottom): the reader's
// least move up stops it, coming back to the bottom resumes it. Plain functions over the scroller, so a test
// can drive them with a fake one.

/** How close to the bottom counts as back at the bottom. */
export const STICK_PX = 40;

/** What this reads and writes of the scrolling element. */
export interface Scroller {
    scrollTop: number;
    readonly scrollHeight: number;
    readonly clientHeight: number;
    readonly style: {overflowAnchor: string};
}

export const follower = () => {
    let following = true;
    let last = 0; // scrollTop after the last scroll seen or made: an event above it is a move up

    // Scroll anchoring keeps a reader's place when the cap drops the oldest row; at the bottom it would move
    // the view up one row instead.
    const anchor = (el: Scroller) => {
        el.style.overflowAnchor = following ? 'none' : 'auto';
    };

    return {
        /** New rows, a resized row or view: back to the bottom if the reader was there. */
        stick(el: Scroller) {
            if (!following) return;
            anchor(el);
            el.scrollTop = el.scrollHeight;
            last = el.scrollTop;
        },
        /**
         * A scroll event. A move up stops following unless it lands on the very bottom (a view that grew, an
         * elastic overscroll settling); our own scroll is no move, even when its event comes after more content.
         */
        scrolled(el: Scroller) {
            const top = el.scrollTop;
            const gap = el.scrollHeight - top - el.clientHeight;
            if (top < last && gap >= 1) following = false;
            else if (gap < STICK_PX) following = true;
            last = top;
            anchor(el);
        }
    };
};
