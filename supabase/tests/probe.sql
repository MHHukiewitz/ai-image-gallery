begin;
select plan(1);
select ok(true, "probe");
select * from finish();
rollback;
