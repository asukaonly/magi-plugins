# Icon source

The icon is from [Microsoft Fluent System Icons](https://github.com/microsoft/fluentui-system-icons),
licensed under the MIT License (see `LICENSE.fluent-icons`).

- Upstream revision: `a563cf9166f4f91aa617557ed272612b7f0a2f72`
- Source: https://raw.githubusercontent.com/microsoft/fluentui-system-icons/a563cf9166f4f91aa617557ed272612b7f0a2f72/assets/Document%20Folder/SVG/ic_fluent_document_folder_24_color.svg
- Retrieved: 2026-09-26

`icon.source.svg` preserves the original 24 px artwork. `icon.png` is its
144 x 144 transparent render, produced with `@resvg/resvg-js` 2.6.2 using
`new Resvg(svg, { fitTo: { mode: "width", value: 144 } }).render().asPng()`.
Colors, geometry, and view box are unchanged. PNG preserves the source gradients
within Magi's existing safe icon format rules. Both files ship with the plugin;
the runtime uses only the PNG and does not request remote artwork.
