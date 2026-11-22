// A value the user is still changing (a slider dragged, or stepped with the keyboard), sent debounced and one
// request at a time, the latest value next: two PUT /api/volume in flight together can reach the daemon in
// either order, since the Mac handles them in a thread pool.

/**
 * Returns `push(value, send, settled)`. `send(value)` runs once the value has been left alone for `delayMs`
 * and no request is in flight; a newer value replaces one still waiting. `settled` runs when the request ends
 * with nothing left to send.
 */
export const coalescer = <T>(delayMs: number) => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let busy = false;
    let next: (() => void) | null = null; // sends the latest value once the request in flight ends

    return (value: T, send: (value: T) => Promise<unknown>, settled: () => void) => {
        const start = () => {
            busy = true;
            const done = () => {
                busy = false;
                const queued = next;
                next = null;
                if (queued) queued();
                else if (timer === undefined) settled();
            };
            send(value).then(done, done);
        };
        clearTimeout(timer);
        timer = setTimeout(() => {
            timer = undefined;
            if (busy) next = start;
            else start();
        }, delayMs);
    };
};
