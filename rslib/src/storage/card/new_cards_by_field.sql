WITH ranked AS MATERIALIZED (
  SELECT c.id,
    c.nid,
    c.ord,
    cast(c.mod AS integer) AS mtime,
    c.did,
    c.odid,
    c.queue = -1 AS suspended,
    field_sort_value(
      CASE
        WHEN ?1 = '' THEN cast(n.sfld AS text)
        WHEN f.ord IS NOT NULL THEN field_at_index(n.flds, f.ord)
      END,
      ?1 != ''
    ) AS value
  FROM (
      SELECT *
      FROM cards
      WHERE did IN (
          SELECT id
          FROM active_decks
        )
        AND (CANDIDATE_FILTER)
    ) c
    JOIN notes n ON n.id = c.nid
    LEFT JOIN fields f ON f.ntid = n.mid
    AND f.name = ?1
)
SELECT id,
  nid,
  ord,
  mtime,
  did,
  odid,
  suspended
FROM ranked
ORDER BY GROUP_ORDER value IS NULL,
  typeof(value) = 'text',
  value COLLATE unicase FIELD_DIRECTION,
  nid,
  ord,
  id