/**
 * The <option> list for a country <select>: the pinned domiciles (United
 * States, Canada), a separator rule, then every other country alphabetically.
 *
 * A component rather than three copies of the same map, so the pinned set and
 * the separator can't drift between the onboarding, organization and party
 * forms — they are the same picker as far as the user is concerned.
 *
 * The caller still supplies its own leading placeholder <option>, because the
 * wording differs by form ("Select country…" where the field is part of an
 * address, "—" where it is an optional standalone field).
 */
import { COUNTRIES_PINNED, COUNTRIES_REST, COUNTRY_SEPARATOR } from "../countries";

export default function CountryOptions() {
  return (
    <>
      {COUNTRIES_PINNED.map(c => <option key={c.code} value={c.name}>{c.name}</option>)}
      {/* Not selectable, and carries no value: it is a rule, not a country.
          `disabled` is what stops keyboard/type-ahead landing on it. */}
      <option disabled value="">{COUNTRY_SEPARATOR}</option>
      {COUNTRIES_REST.map(c => <option key={c.code} value={c.name}>{c.name}</option>)}
    </>
  );
}
