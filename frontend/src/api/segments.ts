/**
 * The carrier's own business segments.
 *
 * The programme form used to offer five hard-coded names while the stored
 * column is free text — so a carrier writing anything else had no way to say
 * so. These are the carrier's list, kept in ref_code_list/ref_code_value.
 */
import { api } from "./client";
import { currentMga } from "../auth";

export type Segment = { id: number; name: string };

export const getSegments = () =>
  api.get<Segment[]>("/business-segments", { params: { mga: currentMga() } })
     .then(r => r.data);

export const addSegment = (name: string) =>
  api.post<Segment>("/business-segments", { name }, { params: { mga: currentMga() } })
     .then(r => r.data);

/** Retired, not deleted: programmes store the NAME, so history stays readable. */
export const retireSegment = (id: number) =>
  api.delete(`/business-segments/${id}`, { params: { mga: currentMga() } })
     .then(r => r.data as { ok: boolean; name: string; programmes_still_using: number });
