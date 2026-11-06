#!/usr/bin/env python3
"""
Make a DAVAI Pro or Pro Max code.

  export DAVAI_PLAN_SECRET='the same secret as on the server'
  python3 make_pro_code.py pro 30        -> Pro for 30 days
  python3 make_pro_code.py promax 30     -> Pro Max for 30 days

Give the printed code to the student. They paste it in DAVAI → Settings → Your plan.
Keep DAVAI_PLAN_SECRET private: anyone with it can make free Pro codes.
"""
import sys, time
import proxy

def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("pro", "promax"):
        print("Use:  python3 make_pro_code.py pro|promax DAYS"); sys.exit(1)
    try:
        days = int(sys.argv[2])
        assert 1 <= days <= 400
    except Exception:
        print("DAYS must be a number from 1 to 400"); sys.exit(1)
    try:
        code = proxy.make_code(sys.argv[1], days)
    except ValueError as e:
        print(e); sys.exit(1)
    plan = proxy.PLAN_NAMES[sys.argv[1]]
    until = time.strftime("%d %b %Y", time.localtime(time.time() + days * 86400))
    print(f"\n{plan} code ({proxy.PLAN_LIMITS[sys.argv[1]]} questions a day), valid until {until}:\n\n{code}\n")

if __name__ == "__main__":
    main()
