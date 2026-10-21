-- "Find similar" hides matches that share nothing but colors. A score is
-- 0.7 · meaning (embedding or tags) + 0.3 · color similarity, so a palette alone scores at most 0.3.
-- Keeping only scores above 0.3 means every result shares some meaning with the target; the grid's
-- badge then reads 31% or more. The old floor, 0.1, let 11% matches through, which read as noise.
-- similar_images reads the floor from this function, so only the function changes, and CREATE OR
-- REPLACE keeps its grants from 20261020000005_search.sql.
create or replace function public.search_similar_min_score() returns float8
language sql immutable parallel safe
return 0.3;
