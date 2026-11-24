function breadcrumbRound(selectedRound, selectedRun, runRecord, windowView) {
  if (selectedRun) {
    const value = runRecord && runRecord.round != null ? Number(runRecord.round) : null;
    return value != null && !Number.isNaN(value) ? value : null;
  }
  if (windowView) return null;
  const value = selectedRound != null ? Number(selectedRound) : null;
  return value != null && !Number.isNaN(value) ? value : null;
}

const cases = [
  {args: [2, 'last', {round: 4}, false], want: 4},
  {args: [2, 'run-9', {round: 4}, false], want: 4},
  {args: [2, 'last', {}, false], want: null},
  {args: [2, null, null, true], want: null},
  {args: [2, null, null, false], want: 2},
  {args: [null, null, null, false], want: null},
];
for (const item of cases) {
  const got = breadcrumbRound(...item.args);
  if (got !== item.want) {
    console.error('FAIL', item, 'got', got);
    process.exit(1);
  }
}
console.log('PASS breadcrumbRound', cases.length, 'cases');
