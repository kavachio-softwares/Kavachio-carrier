/**
 * Things that are built and working but deliberately not on screen yet.
 *
 * A flag here is a DEMO SWITCH, not a half-finished branch. The code behind it
 * is complete and tested; it is off only because somebody asked for it to be
 * off in front of a particular audience. Turning it back on is changing the
 * value — there is no commented-out code anywhere to find and restore, and
 * nothing downstream reads these, so the server behaves the same either way.
 */

/**
 * The "Create Output BDX Template" flow — building a template from a reporting
 * standard or from the contract, instead of uploading one you were sent.
 *
 * OFF at the user's request (3 Sep 2026) so a senior demo does not show it yet.
 *
 * While it is off:
 *   · the "Create BDX Template" button on the Output Template card is gone,
 *     and the card behaves as it did before — click anywhere to upload;
 *   · the create/replace buttons in the output-template state box are gone;
 *   · the dialog is not mounted at all.
 *
 * UPLOADING an output template is untouched and stays available throughout, so
 * a setup can still be completed the ordinary way. Set this to `true` to bring
 * the whole flow back.
 */
export const SHOW_BDX_TEMPLATE_BUILDER = false;
