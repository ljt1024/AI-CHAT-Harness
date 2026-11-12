import { useEffect, useRef, type RefObject } from 'react';

/**
 * Keeps a filter field showing only what the reader typed.
 *
 * A password manager cannot tell a filter from a login box: given a text field
 * with no `name` and no `autocomplete`, Chrome and Safari fill the saved account
 * name in on page load. In a filter that is not a cosmetic problem -- the value
 * narrows the list to nothing, so "my account is admin" arrives on screen as
 * "everything disappeared", with the cause sitting in an input nobody suspects.
 *
 * `autocomplete="off"` and the vendor ignore flags on the element stop most of
 * it. This covers the rest: a manager writes into a field it never focused, and
 * a person cannot, so any value that disagrees with the state is put back to it.
 *
 * Native listeners rather than React's onChange: a programmatic write can be
 * deduped by React's value tracker, and this has to see every one of them.
 */
export function useFilterGuard(
  ref: RefObject<HTMLInputElement | null>,
  value: string,
  clear: () => void,
): void {
  // The setter changes identity on every render; the listener must not.
  const clearRef = useRef(clear);
  clearRef.current = clear;
  const valueRef = useRef(value);
  valueRef.current = value;
  const watched = useRef<HTMLInputElement | null>(null);

  // Deliberately no dependency array. Filter fields are usually rendered behind
  // a condition -- "once the catalogue arrived", "once the sidebar search is
  // open" -- so at mount there is no element to attach to. This re-checks after
  // every render and attaches to whatever field is there now.
  useEffect(() => {
    const field = ref.current;
    if (!field || field === watched.current) return;

    const settle = () => {
      if (field.value !== '' && document.activeElement !== field) {
        field.value = '';
        clearRef.current();
      }
    };
    // Clicking a field never changes what it holds, so on focus the state is the
    // only truth -- this undoes a fill that arrived without an event at all.
    const reconcile = () => {
      if (field.value !== valueRef.current) field.value = valueRef.current;
    };
    // A field that was already filled while nobody was looking: attach time is
    // the only moment this is observable.
    if (field.value !== valueRef.current) settle();
    field.addEventListener('input', settle);
    field.addEventListener('change', settle);
    field.addEventListener('focus', reconcile);
    watched.current = field;

    return () => {
      field.removeEventListener('input', settle);
      field.removeEventListener('change', settle);
      field.removeEventListener('focus', reconcile);
      watched.current = null;
    };
  });
}
