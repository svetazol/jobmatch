/**
 * What the company card and the identity under it both need: deciding what
 * is worth a link, and whether a name says anything the page already shows.
 */

/** The host alone, which is what you would type; the link keeps the full URL. */
export const host = (url: string) => {
  try { return new URL(url).host.replace(/^www\./, '') } catch { return url }
}

/** Only http(s) becomes a link: Vue renders a `javascript:` href as given.
 *  The API already drops those; this is the second lock on the same door. */
export const isWeb = (url: string | null): url is string => !!url && /^https?:\/\//i.test(url)

/** "Okko" and "OKKO " are one name: repeating it would read as news. */
export const sameName = (a: string | null, b: string | null) =>
  (a ?? '').toLowerCase().replace(/\s+/g, '') === (b ?? '').toLowerCase().replace(/\s+/g, '')
