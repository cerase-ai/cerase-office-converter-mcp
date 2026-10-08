-- render_document's pandoc filter.
--
-- The Markdown is untrusted: an assistant may be repeating a document it was
-- given. Pandoc reads it with raw HTML off, which turns <script> and <iframe>
-- into text, but an attribute block such as {onclick="..."} and a javascript:
-- link still reach the HTML. So:
--   * an element keeps only the attributes a document needs for layout;
--   * a link keeps its target only for http(s), mailto, tel or an anchor in
--     the page, and otherwise prints its text;
--   * an image is kept only by https URL or as a data: image, and otherwise
--     prints its alt text.
--
-- It also builds the title block's label and value pairs from the YAML block,
-- with labels in the document's `lang`.

local KEEP = { width = true, height = true, style = true, lang = true, dir = true, title = true }

local function clean_attributes(el)
  if not el.attributes then
    return nil
  end
  local drop = {}
  for key, _ in pairs(el.attributes) do
    if not KEEP[key:lower()] then
      table.insert(drop, key)
    end
  end
  if #drop == 0 then
    return nil
  end
  for _, key in ipairs(drop) do
    el.attributes[key] = nil
  end
  return el
end

local function link_allowed(target)
  local t = target:lower()
  return t:match("^https?://") or t:match("^mailto:") or t:match("^tel:") or t:match("^#")
end

local function image_allowed(src)
  local s = src:lower()
  return s:match("^https://") or s:match("^data:image/")
end

function Link(el)
  if not link_allowed(el.target) then
    return el.content
  end
  clean_attributes(el)
  return el
end

function Image(el)
  if not image_allowed(el.src) then
    return el.caption
  end
  clean_attributes(el)
  return el
end

function Inline(el)
  return clean_attributes(el)
end

function Block(el)
  return clean_attributes(el)
end

local LABELS = {
  it = { client = "Cliente", recipient = "Destinatario", reference = "Riferimento", number = "Numero", date = "Data", author = "A cura di" },
  en = { client = "Client", recipient = "To", reference = "Reference", number = "Number", date = "Date", author = "Prepared by" },
  fr = { client = "Client", recipient = "Destinataire", reference = "Référence", number = "Numéro", date = "Date", author = "Préparé par" },
  de = { client = "Kunde", recipient = "An", reference = "Referenz", number = "Nummer", date = "Datum", author = "Erstellt von" },
  es = { client = "Cliente", recipient = "Destinatario", reference = "Referencia", number = "Número", date = "Fecha", author = "Preparado por" },
}

-- Who the document is for prints in a column of its own; the other fields
-- print beside it as a list of label and value, in this order.
local PARTIES = { "client", "recipient" }
local DETAILS = { "reference", "number", "date", "author" }

-- An address written on several lines keeps its lines.
local function keep_lines(value)
  if value.walk then
    return value:walk({ SoftBreak = function() return pandoc.LineBreak() end })
  end
  return value
end

local function fields(meta, keys, labels)
  local list = pandoc.List()
  for _, key in ipairs(keys) do
    local value = meta[key]
    if value ~= nil and pandoc.utils.stringify(value) ~= "" then
      if pandoc.utils.type(value) == "List" then
        local names = {}
        for _, item in ipairs(value) do
          table.insert(names, pandoc.utils.stringify(item))
        end
        value = table.concat(names, ", ")
      else
        value = keep_lines(value)
      end
      list:insert({ key = key, label = labels[key], value = value })
    end
  end
  return list
end

function Meta(meta)
  local lang = pandoc.utils.stringify(meta.lang or ""):lower():sub(1, 2)
  local labels = LABELS[lang] or LABELS.en
  local parties = fields(meta, PARTIES, labels)
  local details = fields(meta, DETAILS, labels)
  if #parties > 0 then
    meta["title-block-parties"] = parties
  end
  if #details > 0 then
    meta["title-block-details"] = details
  end
  if #parties > 0 or #details > 0 then
    meta["title-block-fields"] = true
  end
  if meta.title ~= nil or #parties > 0 or #details > 0 then
    meta["title-block"] = true
  end
  return meta
end
