# UI design reference (PART 0 one-off fetch)

Source: public homepage https://wallex.ir plus 2 linked stylesheet(s), fetched once with plain GETs.

- Default theme: **dark**
- Top colors by frequency: #252525 (x53), #007aff (x41), #ffffff (x30), #171d26 (x28), #d7dadf (x26), #f5f6f7 (x12), #e5f2ff (x12), #f65555 (x9), #4c9aff (x8), #ebedef (x7), #f7f7f7 (x5), #2e3a4d (x3)
- Font families: iransansxv (x3), iransansx (x3), iransansxv Fallback (x1), inherit (x1), inherit!important (x1)
- border-radius values: -
- box-shadow style: soft low-opacity dark shadows
- Color roles: background: #ffffff (x30), #d7dadf (x26), #f5f6f7 (x12), text: #252525 (x53), #171d26 (x28), #1d2430 (x1), mid/accent: #007aff (x41), #f65555 (x9), #4c9aff (x8), #2e3a4d (x3), #266798 (x1), #039874 (x1)
- Layout traits: modern exchange SPA: fixed header bar, card-based sections; dense numeric tables, side padding roughly 16-24px; buttons/pills use full rounding, cards use small radii

Derived palette for the dashboard (no logos, wordmarks or page text
copied; the dashboard is its own product):
- background/surface/text/primary/accent/success/danger derived from the
  color roles above (or the neutral fintech fallback if the fetch failed).

_No page content, CSS, images or fonts were saved or embedded._