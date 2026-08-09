"""What moved. Reads the anchor-status history and reports transitions.

The capture in `tools/sweep.py` writes one row per item and store per run,
whether or not there is a clearance price. This reads those rows back and
answers the only question that matters about them: does a state predict a
markdown?

Run it against the collector's database (an SSH tunnel is fine):

    PENNYRUN_DB_URL=... python3 -m tools.statuswatch

A state here is one of:

    PRICED      on clearance, with a price
    FLAGGED     the store says CLEARANCE, the price feed does not agree
    ACTIVE      normally stocked, not discounted
    INACTIVE    not discounted and not normally stocked either
    NA          the store returned a status we have not seen before

The transition worth watching is anything -> PRICED at the SAME store.
FLAGGED -> PRICED would mean the flag runs ahead of the price feed, which
would make those pairs worth driving to before anyone else can see them.
INACTIVE -> PRICED would mean an item leaving a store's range tends to get
marked down there, which is where penny items come from.

Careful with the obvious wrong test: nearly every item we price is on
clearance at SOME store, because that is how it got on the hot list. Asking
"is this item on clearance somewhere?" returns ~100% for every state and
proves nothing. The comparison has to be the same item at the same store,
across time -- which is why this needs history and could not be answered
the day the signal was found.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STATE = """
case when o.clearance_price is not null then 'PRICED'
     when o.anchor_status = 'CLEARANCE'  then 'FLAGGED'
     else coalesce(o.anchor_status, 'NA') end
"""

# One row per item, store and run, paired with the state it was in the run
# before. Deliberately consecutive OBSERVATIONS, not calendar days: the
# database is UTC and the sweep fires at 05:10 local, so a run and the one
# before it can land either side of a UTC midnight or share one -- grouping
# by day silently merged two runs into one and reported no changes at all.
# The elapsed gap is carried so a three-hour comparison is never read as a
# day-over-day one.
MOVES = f"""
with seen as (
  select o.item_id, o.store_id, o.observed_at, {STATE} as state
    from observation o
   where o.trusted
),
moved as (
  select item_id, store_id, observed_at, state,
         lag(state)       over w as was,
         lag(observed_at) over w as was_at
    from seen
  window w as (partition by item_id, store_id order by observed_at)
)
select was, state, count(*) as n,
       round(avg(extract(epoch from (observed_at - was_at)) / 3600.0)::numeric, 1) as hours
  from moved
 where was is not null and was <> state
 group by 1, 2
 order by n desc
"""

COVERAGE = """
select date_trunc('day', observed_at)::date as day,
       count(*) filter (where clearance_price is not null) as priced,
       count(*) filter (where clearance_price is null)     as unpriced
  from observation where trusted
 group by 1 order by 1 desc limit 10
"""


def main():
    from api.db import rows

    with rows() as cur:
        cur.execute(COVERAGE)
        days = cur.fetchall()
        print("observations per day (unpriced rows are the new history):")
        for d in days:
            print("  %s  priced %6d   unpriced %6d" % (d["day"], d["priced"], d["unpriced"]))

        cur.execute(MOVES)
        moves = cur.fetchall()

    if not moves:
        print("\nNo state changes yet.")
        return

    print("\nstate changes, same item at the same store, run over run:")
    to_priced = [m for m in moves if m["state"] == "PRICED"]
    for m in moves:
        mark = "   <-- a markdown appeared" if m["state"] == "PRICED" else ""
        print("  %-9s -> %-9s %6d   avg %sh apart%s"
              % (m["was"], m["state"], m["n"], m["hours"], mark))

    if to_priced:
        print("\nwhich states turn into a markdown, and how often:")
        starts = {}
        for m in moves:
            starts[m["was"]] = starts.get(m["was"], 0) + m["n"]
        for m in sorted(to_priced, key=lambda x: -x["n"]):
            total = starts.get(m["was"], 0)
            print("  %-9s -> PRICED : %d of %d changes from that state (%.0f%%)"
                  % (m["was"], m["n"], total, 100.0 * m["n"] / max(total, 1)))
        print("\nA state that leads to PRICED much more often than the others is a"
              "\nlead worth surfacing in the app. One night is not enough to say.")


if __name__ == "__main__":
    main()
