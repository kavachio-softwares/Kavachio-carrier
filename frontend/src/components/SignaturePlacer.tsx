/**
 * Put the signature blocks where you want them, by dragging them there.
 *
 * The two automatic arrangements answer "side by side or one above the other?"
 * — which is the whole question for most contracts and none of it for the rest:
 * a block that has to sit beside a particular clause, or under a schedule, or
 * where the house style has always put it, cannot be expressed as a choice
 * between two shapes. This is the third answer, and it is the one nobody can
 * anticipate for somebody else.
 *
 * WHAT IS BEING DRAGGED. Not a picture of a block — the block. Its size comes
 * from the server (`placed_block`), the page images are the composed contract
 * itself, and the position saved is the position drawn: a fraction of the page,
 * top-left origin, which is the same coordinate system the signing round reads
 * its boxes in. So what is on the screen is what comes out of the printer, and
 * a signing box lands exactly where the block was left.
 *
 * WHY THE PAGES ARE FETCHED AS BLOBS. Every call to this API carries a bearer
 * token and an <img src> cannot set a header, so each page is fetched and shown
 * from an object URL — which is also why they are revoked on the way out.
 */
import { useEffect, useRef, useState } from "react";
import {
  getContractPageImage, getContractPages,
  type SignatureLayout,
} from "../api/contractRecord";

type Spot = { page: number; x: number; y: number };

export function SignaturePlacer({
  contractId, layout, sides, block, onPlace,
}: {
  contractId: number;
  layout: SignatureLayout;
  /** The two sides, in the words this contract uses for them. */
  sides: Array<{ key: string; label: string }>;
  /** Block size as a fraction of the page — served, never guessed here. */
  block: { width: number; height: number };
  onPlace: (side: string, spot: Spot) => void;
}) {
  const [pages, setPages] = useState<
    Array<{ width: number; height: number }> | null>(null);
  const [images, setImages] = useState<Record<number, string>>({});
  const [err, setErr] = useState("");
  // Which block the next click on a page places. The one still unplaced, by
  // default — placing is the thing somebody came here to do, and asking them to
  // choose a side first when only one of the two is missing is a question with
  // one answer.
  const [arming, setArming] = useState<string | null>(null);
  const dragging = useRef<
    { side: string; page: number; dx: number; dy: number } | null>(null);

  useEffect(() => {
    let stale = false;
    const made: string[] = [];
    setErr("");
    getContractPages(contractId)
      .then(async info => {
        if (stale) return;
        setPages(info.sizes);
        // Sequentially: each page composes the whole contract server-side, and
        // firing eight of those at once to show a document nobody has scrolled
        // to yet is not worth the pause it puts on everything else.
        for (let n = 1; n <= info.pages; n++) {
          if (stale) return;
          try {
            const url = await getContractPageImage(contractId, n);
            // Revoked here rather than added to the list the cleanup walks: by
            // now that cleanup has already run, and a blob handed to nobody is
            // a leak that lives as long as the tab does.
            if (stale) { URL.revokeObjectURL(url); return; }
            made.push(url);
            setImages(m => ({ ...m, [n]: url }));
          } catch { /* one page failing is not the screen failing */ }
        }
      })
      .catch(() => !stale && setErr(
        "The contract could not be laid out for placing. It has to have a "
        + "wording before there is a page to put a block on."));
    return () => {
      stale = true;
      made.forEach(u => URL.revokeObjectURL(u));
    };
  }, [contractId]);

  // Armed ONCE, on the way in, with whichever side has nowhere to go. After
  // that it is the buttons' to set: re-deriving it from the blocks would
  // disarm "Move this one" the instant it was pressed, because a block being
  // moved is a block that is already placed.
  const armed = useRef(false);
  useEffect(() => {
    if (armed.current) return;
    armed.current = true;
    setArming(sides.find(s => !layout.blocks?.[s.key])?.key ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Where a point on a page is, as a fraction of it. */
  function at(e: { clientX: number; clientY: number }, el: HTMLElement) {
    const r = el.getBoundingClientRect();
    return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
  }

  /** Keep a block whole on the page: a top-left corner far enough right or far
   *  enough down would hang the block off the paper, and the drawer would pull
   *  it back — so the screen refuses to put it there in the first place. */
  function clamp(x: number, y: number): { x: number; y: number } {
    return {
      x: Math.max(0, Math.min(x, 1 - block.width)),
      y: Math.max(0, Math.min(y, 1 - block.height)),
    };
  }

  if (err) return <div className="note warn">{err}</div>;
  if (!pages) return <div className="empty">Laying the contract out…</div>;

  return (
    <div>
      <div className="rowacts" style={{ marginBottom: 10 }}>
        {sides.map(s => {
          const spot = layout.blocks?.[s.key];
          return (
            <button
              key={s.key} type="button"
              className={`btn sm${arming === s.key ? " pri" : ""}`}
              onClick={() => setArming(a => (a === s.key ? null : s.key))}
            >
              {spot ? `Move ${s.label}` : `Place ${s.label}`}
              {spot && <span className="sub"> · page {spot.page}</span>}
            </button>
          );
        })}
        <span className="sub">
          {arming
            ? "Click the page where it should go — or drag a block that is "
              + "already there."
            : "Both blocks are placed. Drag either one to move it."}
        </span>
      </div>

      <div className="place-doc">
        {pages.map((size, i) => {
          const n = i + 1;
          const here = sides
            .map(s => ({ side: s, spot: layout.blocks?.[s.key] }))
            .filter(b => b.spot && b.spot.page === n);
          return (
            <div
              key={n} className="place-pg"
              style={{ aspectRatio: `${size.width} / ${size.height}` }}
              onClick={e => {
                if (!arming) return;
                const p = at(e, e.currentTarget as HTMLElement);
                // Dropped where the pointer is, not where its top-left would
                // be: somebody points at where they want the block, so the
                // block arrives centred under the finger.
                onPlace(arming, {
                  page: n,
                  ...clamp(p.x - block.width / 2, p.y - block.height / 2),
                });
                setArming(null);
              }}
            >
              {images[n]
                ? <img src={images[n]} alt={`Page ${n}`} draggable={false} />
                : <div className="ld">Page {n}…</div>}
              <span className="no">{n}</span>

              {here.map(({ side, spot }) => (
                <div
                  key={side.key}
                  className="place-blk"
                  style={{
                    left: `${(spot as Spot).x * 100}%`,
                    top: `${(spot as Spot).y * 100}%`,
                    width: `${block.width * 100}%`,
                    height: `${block.height * 100}%`,
                  }}
                  onClick={e => e.stopPropagation()}
                  onPointerDown={e => {
                    e.stopPropagation();
                    const pg = (e.currentTarget.parentElement as HTMLElement);
                    const p = at(e, pg);
                    dragging.current = {
                      side: side.key, page: n,
                      dx: p.x - (spot as Spot).x, dy: p.y - (spot as Spot).y,
                    };
                    e.currentTarget.setPointerCapture(e.pointerId);
                  }}
                  onPointerMove={e => {
                    const d = dragging.current;
                    if (!d || d.side !== side.key) return;
                    const pg = (e.currentTarget.parentElement as HTMLElement);
                    const p = at(e, pg);
                    onPlace(side.key,
                            { page: n, ...clamp(p.x - d.dx, p.y - d.dy) });
                  }}
                  onPointerUp={e => {
                    dragging.current = null;
                    e.currentTarget.releasePointerCapture(e.pointerId);
                  }}
                >
                  <b>{side.label}</b>
                  <span className="rule" />
                  <span className="sub">
                    {(layout.fields[side.key] ?? [])
                      .filter(k => k !== "signature").length} line(s) under it
                  </span>
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}
