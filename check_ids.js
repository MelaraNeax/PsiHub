const fs = require('fs');
const html = fs.readFileSync('index.html', 'utf8');
const ids = [
  'explore-topic-avatar','explore-topic-icon','explore-story-topic-label',
  'explore-headline','stat-searches','explore-badge-topic',
  'recs-list','recs-subtitle','btn-refresh-recs',
  'results-loading','results-empty','results-error','results-error-msg',
  'btn-results-retry','paper-list','btn-load-more',
  'page-home','page-explore','page-profile',
  'search-input','corrientes-row','results-view'
];
ids.forEach(id => {
  const found = html.includes('id="' + id + '"');
  console.log((found ? 'OK    ' : 'MISSING') + ': ' + id);
});
