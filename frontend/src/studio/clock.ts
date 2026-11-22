// The studio's playback clock, outside React: the 3D reads it at every frame, the timeline a few times a second.
// It loops: a preview plays until paused.

export class Clock {
    playing = false;
    duration = 0;
    private offset = 0; // seconds, at `since`
    private since = 0;

    constructor(private readonly ms: () => number = () => performance.now()) {}

    /** Seconds into the frames. */
    now(): number {
        if (!this.playing) return this.offset;
        return this.duration > 0 ? (this.offset + (this.ms() - this.since) / 1000) % this.duration : 0;
    }

    /** New frames: from the start, playing. */
    restart(duration: number) {
        this.duration = duration;
        this.seek(0);
        this.playing = true;
    }

    toggle() {
        this.seek(this.now());
        this.playing = !this.playing;
    }

    seek(t: number) {
        this.offset = Math.min(Math.max(t, 0), this.duration);
        this.since = this.ms();
    }
}
