// A streamed draft (sendMessageDraft): an animated, ephemeral preview in a private chat.
// Telegram drops a draft after ~30 seconds, so a live draft is re-sent periodically until it ends.

import type { Telegram } from "./telegram";

const MIN_INTERVAL = 700;
const KEEPALIVE = 20_000;

export class Draft {
  readonly id = 1 + Math.floor(Math.random() * 2_000_000_000);
  private text = "";
  private sent = 0;
  private pending: Timer | undefined;
  private keepalive: Timer | undefined;
  private ended = false;

  constructor(
    private tg: Telegram,
    private chat: number,
    private thread: number | undefined,
    private extra: Record<string, unknown> = {},
  ) {}

  /** Show text (empty = Telegram's "Thinking…" placeholder). Updates are throttled, the latest wins. */
  show(text: string) {
    if (this.ended) return;
    this.text = text.length > 4000 ? `…${text.slice(-3999)}` : text;
    const wait = Math.max(0, this.sent + MIN_INTERVAL - Date.now());
    clearTimeout(this.pending);
    this.pending = setTimeout(() => this.push(), wait);
  }

  private push() {
    if (this.ended) return;
    this.sent = Date.now();
    this.tg
      .call("sendMessageDraft", {
        chat_id: this.chat,
        message_thread_id: this.thread,
        draft_id: this.id,
        text: this.text,
        can_stop: true,
        ...this.extra,
      })
      .catch((e) => console.error(String(e)));
    clearTimeout(this.keepalive);
    this.keepalive = setTimeout(() => this.push(), KEEPALIVE);
  }

  /** Stop updating; the next real message from the bot replaces the draft. */
  end() {
    this.ended = true;
    clearTimeout(this.pending);
    clearTimeout(this.keepalive);
  }

  get active() {
    return !this.ended;
  }
}
