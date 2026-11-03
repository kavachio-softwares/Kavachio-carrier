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
 * THE WORDING MOVES OUT OF THE WAY. A block used to be drawn on top of the
 * finished page, which meant it could — and on a full page always did — land
 * across the middle of a clause. It is not drawn on top any more: a drop is
 * resolved to a place in the document's FLOW (the mark just above it, plus the
 * distance below that mark), and the block is typeset there. So the wording
 * below it moves down to make room, exactly the way it does for a picture
 * dropped into a word processor, and a signature can no longer cover the
 * clause it is agreeing to. Nothing to warn about, because nothing can go
 * wrong: see contract_wording.FlowMark for the measuring that makes it work.
 *
 * ONE BLOCK PER PERSON. A target is a party key — `carrier` for the side and
 * whoever signs for it first, `carrier#2` for the next person named — which is
 * the same spelling the signing round keys its boxes by, so a block dragged for
 * one person and the boxes that person ends up signing in cannot drift apart.
 * A side that names three people offers three blocks; drag all three and they
 * land in three places, drag none of them and they stack under the side exactly
 * as they always did. That is why placing anybody but the side itself is
 * optional: the old behaviour is what you get by not using the new one.
 *
 * A SIDE MAY BE TIED TO A CLAUSE INSTEAD. Same reflow, chosen by name rather
 * than by pointing: "after 4. Financial terms" rather than a spot. For a block
 * that belongs to a clause wherever that clause ends up, which is not the same
 * wish as putting one at a point.
 *
 * WHY THE PAGES ARE FETCHED AS BLOBS. Every call to this API carries a bearer
 * token and an <img src> cannot set a header, so each page is fetched and shown
 * from an object URL — which is also why they are revoked on the way out.
 */
import { useEffect, useRef, useState } from "react";
import { Minus, Plus, X } from "lucide-react";
import {
  getContractPageImage, getContractPages,
  getDraftPageImage, getDraftPages, isDropSpot,
  type BlockSpot, type FlowMark, type SignatureLayout, type WordingInput,
} from "../api/contractRecord";

/** The sizes a page is offered at. 1 is "fits the column"; the rest scroll. */
const ZOOMS = [1, 1.5, 2, 2.75, 3.5];

/** One block that can be placed. `key` is the party key it is saved under. */
export type PlaceTarget = {
  key: string;
  label: string;
  /** Who this block is for, in a word or two — shown under the label. */
  hint?: string;
  /** Whether the layout cannot be saved without it. A SIDE must be placed; a
   *  person named for that side need not be, because not placing them is how
   *  you ask for them to stack. */
  required?: boolean;
};

/**
 * Where the pages being dragged onto come from.
 *
 * A SAVED contract is an id. One still being written in the create wizard is
 * its terms — and the server composes the same document from either, which is
 * the whole reason the blocks can be placed before anything is saved.
 */
export type PlaceSource =
  | { kind: "contract"; id: number }
  | { kind: "draft"; key: string; body: WordingInput };

/** The side a party key belongs to, whichever of its people it points at.
 *  The same split the server does — see esign_pdf.base_key. */
const baseKey = (key: string) => key.split("#", 1)[0];

/**
 * The blocks a contract can have placed: each side, and each person named to
 * sign for it.
 *
 * ONE function for every screen that offers placing. Two screens deriving
 * these keys separately is two chances to number a slot differently, and a
 * block saved under a key the other screen does not offer is a block nobody
 * can move again.
 *
 * The slots MUST match the server's, which numbers the people it actually
 * draws — so the same empty names are dropped here, in the same order, before
 * anything is counted. Slot 1 is the side key itself, which is why a side that
 * names nobody still offers exactly one block.
 */
export function signerTargets(
  sides: readonly string[],
  signers: ReadonlyArray<{ name?: string; role?: string; side: string }>
    | null | undefined,
  labelFor: (side: string) => string,
): PlaceTarget[] {
  return sides.flatMap((side): PlaceTarget[] => {
    const sideLabel = labelFor(side);
    const people = (signers ?? [])
      .filter(sg => sg.side === side && (sg.name ?? "").trim());
    // Only the SIDE is required. Somebody named but never dragged stacks under
    // their side exactly as they used to — not placing them is how you ask for
    // that, so demanding it would take the old behaviour away.
    if (!people.length) return [{ key: side, label: sideLabel, required: true }];
    return people.map((sg, i) => ({
      key: i === 0 ? side : `${side}#${i + 1}`,
      label: (sg.name ?? "").trim() || sideLabel,
      hint: [sg.role, sideLabel].filter(Boolean).join(" · "),
      required: i === 0,
    }));
  });
}

export function SignaturePlacer({
  source, layout, targets, block, onPlace, onRemove, onAnchor,
}: {
  /** The document to drag onto — saved, or still being written. */
  source: PlaceSource;
  layout: SignatureLayout;
  /** Every block that can be placed, in the order they are offered. */
  targets: PlaceTarget[];
  /** Block size as a fraction of the page — served, never guessed here. */
  block: { width: number; height: number };
  /** Put a block down: where it was dropped, as a place in the flow. */
  onPlace: (key: string, spot: BlockSpot) => void;
  /** Take a block back off the page. A placement has to be undoable without
   *  starting the whole screen again. Un-placing a SIDE leaves the layout
   *  unsaveable until it is placed again, which the screen says; un-placing a
   *  person puts them back under their side, stacked, which is where they were
   *  before anybody dragged. */
  onRemove: (key: string) => void;
  /** Tie a side's block to a clause instead of to a point. Omitted, only
   *  dropping is offered. */
  onAnchor?: (key: string, after: number) => void;
}) {
  const [pages, setPages] = useState<
    Array<{ width: number; height: number }> | null>(null);
  const [images, setImages] = useState<Record<number, string>>({});
  const [marks, setMarks] = useState<FlowMark[]>([]);
  const [clauses, setClauses] = useState<Array<{ n: number; title: string }>>([]);
  const [landings, setLandings] =
    useState<Record<string, { page: number; y: number }>>({});
  const [err, setErr] = useState("");
  // Which block the next click on a page places. The one still unplaced, by
  // default — placing is the thing somebody came here to do, and asking them to
  // choose first when only one of them is missing is a question with one
  // answer.
  const [arming, setArming] = useState<string | null>(null);
  // How big the pages are drawn. A page shrunk to fit a column is a picture of
  // a contract, not a contract — the words are the whole reason somebody is
  // looking at it, because "where should this go" is answered by reading the
  // page.
  const [zoom, setZoom] = useState(1);
  const [busy, setBusy] = useState(false);
  const dragging = useRef<
    { key: string; page: number; dx: number; dy: number } | null>(null);
  // WHERE THE BLOCK IS WHILE IT IS BEING DRAGGED, held here rather than pushed
  // up on every mouse move. Telling the parent sixty times a second re-renders
  // the whole screen around this one — a create wizard is a big form — and the
  // box ends up trailing the pointer on a page it should be glued to. The
  // parent is told ONCE, when the block is let go.
  const [drag, setDrag] = useState<
    { key: string; page: number; x: number; y: number } | null>(null);
  // Mirrored, because a pointer handler closes over the render it was made in
  // and "where was it when they let go" must not be one frame stale.
  const dragPos = useRef<{ page: number; x: number; y: number } | null>(null);

  // Read only when a fetch actually starts, so the body is always the current
  // one without its identity re-running the effect on every keystroke.
  const src = useRef(source);
  src.current = source;
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  // The earliest page any block sat on last time the pages were composed.
  // Nothing above a block can have moved, so those pages are still good.
  const wasFrom = useRef(1);

  // ── when the pages are worth composing again ───────────────────────────
  // The blocks are IN the document now, so moving one really does change the
  // pages — but re-composing mid-drag would fetch the page under the pointer
  // sixty times to draw the same words. So the canvas catches up shortly after
  // a block is let go, and the box being dragged is what moves in between.
  const blockSig = JSON.stringify(layout.blocks ?? {});
  const [settled, setSettled] = useState(blockSig);
  useEffect(() => {
    const t = setTimeout(() => setSettled(blockSig), 550);
    return () => clearTimeout(t);
  }, [blockSig]);
  const sourceKey =
    (source.kind === "contract" ? `c${source.id}` : source.key) + settled;

  useEffect(() => {
    let stale = false;
    const made: string[] = [];
    setErr("");
    setBusy(true);
    const s = src.current;
    const lay = layoutRef.current;
    const fetchPages = () => (s.kind === "contract"
      ? getContractPages(s.id, lay) : getDraftPages(s.body));
    const fetchImage = (n: number) => (s.kind === "contract"
      ? getContractPageImage(s.id, n, 2.4, lay)
      : getDraftPageImage(s.body, n, 2.4));
    fetchPages()
      .then(async info => {
        if (stale) return;
        setPages(info.sizes);
        setMarks(info.marks ?? []);
        setClauses(info.clauses ?? []);
        setLandings(info.landings ?? {});
        // ONLY THE PAGES THAT CAN HAVE CHANGED. A block makes room for itself
        // by pushing what comes after it down, so nothing above the earliest
        // block moves — and re-fetching a page to redraw the identical words
        // is the whole of the pause somebody feels after letting go. Measured
        // from both where the blocks are NOW and where they were last time,
        // because a block dragged up the document changes both ends.
        const spots = Object.values(lay.blocks ?? {});
        const dropPages = (info.marks ?? [])
          .filter(m => spots.some(v => (v as { at?: number }).at === m.n))
          .map(m => m.page);
        // Only when EVERY block is a dropped one. A clause anchor moves with
        // its clause and a coordinate is drawn on top, and neither is bounded
        // by a page this can name — so the moment one is in play, everything
        // is composed again. A wrong shortcut here shows somebody stale words.
        const nowFrom = (dropPages.length === spots.length && dropPages.length)
          ? Math.min(...dropPages) : 1;
        const from = Math.max(1, Math.min(nowFrom, wasFrom.current));
        wasFrom.current = nowFrom;
        // Pages that no longer exist go; the rest STAY UP until their
        // replacement is in hand, so the document never blinks out.
        setImages(m => Object.fromEntries(Object.entries(m).filter(
          ([k]) => Number(k) <= info.pages)));
        // Sequentially: each page composes the whole contract server-side, and
        // firing eight of those at once to show a document nobody has scrolled
        // to yet is not worth the pause it puts on everything else.
        for (let n = from; n <= info.pages; n++) {
          if (stale) return;
          try {
            // Rendered well above fit size: these are zoomed into to be READ,
            // and a page fetched at fit size turns to mush at 200%.
            const url = await fetchImage(n);
            // Revoked here rather than added to the list the cleanup walks: by
            // now that cleanup has already run, and a blob handed to nobody is
            // a leak that lives as long as the tab does.
            if (stale) { URL.revokeObjectURL(url); return; }
            made.push(url);
            // The OLD page stays up until its replacement is in hand. Clearing
            // first would blank the document every time a block was let go,
            // which is the moment somebody is looking hardest at it.
            setImages(m => {
              const had = m[n];
              if (had) setTimeout(() => URL.revokeObjectURL(had), 0);
              return { ...m, [n]: url };
            });
          } catch { /* one page failing is not the screen failing */ }
        }
      })
      .catch(() => !stale && setErr(
        "The contract could not be laid out for placing. It has to have a "
        + "wording before there is a page to put a block on."))
      .finally(() => !stale && setBusy(false));
    return () => {
      stale = true;
      made.forEach(u => URL.revokeObjectURL(u));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceKey]);

  // Armed ONCE, on the way in, with whichever block has nowhere to go. After
  // that it is the buttons' to set: re-deriving it from the blocks would
  // disarm "Move this one" the instant it was pressed, because a block being
  // moved is a block that is already placed.
  const armed = useRef(false);
  useEffect(() => {
    if (armed.current) return;
    armed.current = true;
    const missing = targets.filter(t => !layout.blocks?.[t.key]);
    setArming((missing.find(t => t.required) ?? missing[0])?.key ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Where a point on a page is, as a fraction of it. */
  function at(e: { clientX: number; clientY: number }, el: HTMLElement) {
    const r = el.getBoundingClientRect();
    return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
  }

  /**
   * A point on a page, as a place in the document.
   *
   * The mark just above it, and how far below that mark it fell. A typesetter
   * cannot be told "here" — it can only put one thing after another — so this
   * is the whole translation between what somebody points at and what the
   * document can be asked for. Because the answer is exact, the block renders
   * back to the point it was dropped on, and dragging looks like dragging.
   */
  function resolve(page: number, x: number, y: number): BlockSpot | null {
    const here = marks.filter(m => m.page === page);
    // The mark just above the drop, ON THIS PAGE. Falling back to the last
    // mark of the page before would take the block to a page nobody pointed
    // at, so a drop above everything on a page pins to the top of that page
    // instead; only a page with no marks at all looks further back.
    const above = here.filter(m => m.y <= y).pop()
      ?? here[0]
      ?? marks.filter(m => m.page < page).pop()
      ?? marks[0];
    if (!above) return null;
    return {
      at: above.n,
      gap: above.page === page
        ? Math.max(0, Number((y - above.y).toFixed(5))) : 0,
      x: Number(Math.max(0, Math.min(x, 1 - block.width)).toFixed(5)),
    };
  }

  /** Where a block sits on the screen — read back out of the flow position it
   *  was resolved to, so the box and the typeset block are the same place. */
  function screenAt(key: string, spot: BlockSpot | undefined) {
    if (isDropSpot(spot)) {
      // Where it ACTUALLY landed, when the pages have been composed since.
      // A block dropped too near the foot of a page does not fit there and is
      // carried to the next one; following the aim rather than the landing
      // would leave the box claiming a page the block is not on.
      const real = landings[key];
      if (real) return { page: real.page, x: spot.x, y: real.y };
      const m = marks.find(mm => mm.n === spot.at);
      return m ? { page: m.page, x: spot.x, y: m.y + spot.gap } : null;
    }
    // Composed before any of this, and still drawn on top.
    if (spot && "page" in spot) return spot;
    return null;
  }

  /** How many printed lines sit under this block's rule. Read by SIDE — the
   *  fields are chosen per side, and every person signing for it gets the same
   *  lines, so `carrier#2` reads the carrier's answer. */
  const linesUnder = (key: string) =>
    (layout.fields[baseKey(key)] ?? []).filter(k => k !== "signature").length;

  if (err) return <div className="note warn">{err}</div>;
  if (!pages) return <div className="empty">Laying the contract out…</div>;

  const unplacedRequired = targets.filter(
    t => t.required && !layout.blocks?.[t.key]);
  const tiedCount = targets.filter(t => {
    const s = layout.blocks?.[t.key];
    return s && !isDropSpot(s) && "after" in s;
  }).length;

  return (
    <div>
      <div className="rowacts" style={{ marginBottom: 10 }}>
        {targets.map(t => {
          const spot = layout.blocks?.[t.key];
          const tied = spot && !isDropSpot(spot) && "after" in spot
            ? spot.after : null;
          const where = screenAt(t.key, spot);
          // Only a SIDE can be tied to a clause — a side's block carries
          // everybody named for it, so one person out of three has no block of
          // their own to tie. `required` is what marks a side here.
          const mayTie = clauses.length > 0 && !!onAnchor && !!t.required;
          return (
            <span key={t.key} className="place-tgt">
              <button
                type="button"
                className={`btn sm${arming === t.key ? " pri" : ""}`}
                disabled={tied !== null}
                onClick={() => setArming(a => (a === t.key ? null : t.key))}
                title={tied !== null
                  ? "Tied to a clause — choose “a point on the page” to drag it"
                  : t.hint}
              >
                {where ? `Move ${t.label}` : `Place ${t.label}`}
                {where && <span className="sub"> · page {where.page}</span>}
              </button>
              {mayTie && (
                <select
                  className="place-tie" value={tied ?? ""}
                  title={"Tie this side’s block to a clause, so it follows "
                    + "that clause wherever the clause ends up"}
                  onChange={e => {
                    const v = e.target.value;
                    if (!v) { onRemove(t.key); setArming(t.key); }
                    else { onAnchor!(t.key, Number(v)); setArming(null); }
                  }}
                >
                  <option value="">a point on the page</option>
                  {clauses.map(c => (
                    <option key={c.n} value={c.n}>
                      after {c.n}. {c.title}
                    </option>
                  ))}
                </select>
              )}
            </span>
          );
        })}
        <span className="sub">
          {arming
            ? "Click the page where it should go — or drag a block that is "
              + "already there."
            : unplacedRequired.length > 0
              ? `Still to place: ${unplacedRequired
                  .map(t => t.label).join(", ")}.`
              : "Drag any block to move it, or × to take it off the page. "
                + "Anybody left unplaced signs under their side."}
          {tiedCount > 0 && " A block tied to a clause follows that clause."}
        </span>
      </div>

      {/* Reading the page is how somebody decides where a block goes, so the
          page has to be readable. Fit is the start, not the only size. */}
      <div className="rowacts place-zoom">
        <span className="sub">Page size</span>
        <button type="button" className="btn sm" title="Smaller"
                disabled={zoom <= ZOOMS[0]}
                onClick={() => setZoom(z => ZOOMS[
                  Math.max(0, ZOOMS.indexOf(z) - 1)])}>
          <Minus size={12} />
        </button>
        <span className="sub zval">
          {zoom === 1 ? "Fit" : `${Math.round(zoom * 100)}%`}
        </span>
        <button type="button" className="btn sm" title="Bigger"
                disabled={zoom >= ZOOMS[ZOOMS.length - 1]}
                onClick={() => setZoom(z => ZOOMS[
                  Math.min(ZOOMS.length - 1, ZOOMS.indexOf(z) + 1)])}>
          <Plus size={12} />
        </button>
        <span className="sub">
          {busy
            ? "Making room for it in the wording…"
            : "Drop a block anywhere — the wording moves down to make room "
              + "for it, so it never covers a clause."}
        </span>
      </div>

      <div className="place-doc"
           style={{ ["--pg-w" as string]: `${Math.round(520 * zoom)}px` }}>
        {pages.map((size, i) => {
          const n = i + 1;
          const here = targets
            .map(t => ({
              target: t,
              where: drag?.key === t.key
                ? { page: drag.page, x: drag.x, y: drag.y }
                : screenAt(t.key, layout.blocks?.[t.key]),
            }))
            .filter(b => b.where && b.where.page === n);
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
                const spot = resolve(n, p.x - block.width / 2,
                                     p.y - block.height / 2);
                if (spot) onPlace(arming, spot);
                setArming(null);
              }}
            >
              {images[n]
                ? <img src={images[n]} alt={`Page ${n}`} draggable={false} />
                : <div className="ld">Page {n}…</div>}
              <span className="no">{n}</span>

              {here.map(({ target, where }) => (
                <div
                  key={target.key}
                  className="place-blk"
                  style={{
                    left: `${where!.x * 100}%`,
                    top: `${where!.y * 100}%`,
                    width: `${block.width * 100}%`,
                    height: `${block.height * 100}%`,
                  }}
                  onClick={e => e.stopPropagation()}
                  onPointerDown={e => {
                    e.stopPropagation();
                    const pg = (e.currentTarget.parentElement as HTMLElement);
                    const p = at(e, pg);
                    dragging.current = {
                      key: target.key, page: n,
                      dx: p.x - where!.x, dy: p.y - where!.y,
                    };
                    dragPos.current = { page: n, x: where!.x, y: where!.y };
                    setDrag({ key: target.key, page: n,
                              x: where!.x, y: where!.y });
                    e.currentTarget.setPointerCapture(e.pointerId);
                  }}
                  onPointerMove={e => {
                    const d = dragging.current;
                    if (!d || d.key !== target.key) return;
                    const pg = (e.currentTarget.parentElement as HTMLElement);
                    const p = at(e, pg);
                    // Local only — the box follows the finger and nothing else
                    // on the screen has to think about it.
                    const to = {
                      page: n,
                      x: Math.max(0, Math.min(p.x - d.dx, 1 - block.width)),
                      y: Math.max(0, Math.min(p.y - d.dy, 1 - block.height)),
                    };
                    dragPos.current = to;
                    setDrag({ key: target.key, ...to });
                  }}
                  onPointerUp={e => {
                    const d = dragging.current;
                    dragging.current = null;
                    e.currentTarget.releasePointerCapture(e.pointerId);
                    // ONE commit, on release: the block takes its place in the
                    // flow, and the pages catch up a moment later.
                    const to = dragPos.current;
                    dragPos.current = null;
                    if (d && to) {
                      const spot = resolve(to.page, to.x, to.y);
                      if (spot) onPlace(target.key, spot);
                    }
                    setDrag(null);
                  }}
                >
                  <span
                    className="rm" role="button" title="Take this off the page"
                    onPointerDown={e => e.stopPropagation()}
                    onClick={e => { e.stopPropagation(); onRemove(target.key); }}
                  >
                    <X size={11} />
                  </span>
                  <b>{target.label}</b>
                  <span className="rule" />
                  <span className="sub">
                    {target.hint
                      ? target.hint
                      : `${linesUnder(target.key)} line(s) under it`}
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
