// Shared "find a specialist near you" widget used by both apps.
// Expects markup with: #find-doctors (button), #area (select), #doctor-specialty (select),
// #doctor-results (container), #doctor-status (text). Location is only requested on click.
(() => {
  const btn = document.getElementById('find-doctors');
  if (!btn) return;
  const area = document.getElementById('area');
  const specialty = document.getElementById('doctor-specialty');
  const results = document.getElementById('doctor-results');
  const statusEl = document.getElementById('doctor-status');
  const endpoint = btn.dataset.endpoint;

  const text = (tag, value, cls) => {
    const el = document.createElement(tag);
    el.textContent = value;
    if (cls) el.className = cls;
    return el;
  };

  function render(items) {
    results.replaceChildren();
    if (!items.length) {
      results.append(text('p', 'No clinicians found for that choice yet.', 'muted'));
      return;
    }
    for (const d of items) {
      const card = document.createElement('div');
      card.className = 'doctor';
      card.append(text('strong', d.name));
      card.append(text('span', `${d.specialty_label} · about ${d.distance_km} km away`, 'muted block'));
      if (d.address) card.append(text('span', d.address, 'muted block'));
      if (d.hours) card.append(text('span', `Hours: ${d.hours}`, 'muted block'));
      if (d.rating) card.append(text('span', `★ ${d.rating}${d.rating_count ? ` (${d.rating_count} reviews on Google)` : ''}`, 'muted block small'));
      if (d.demo) card.append(text('span', 'Demo entry, not a real clinic', 'pill warn'));
      else if (d.verified_on) card.append(text('span', `Details verified ${d.verified_on}`, 'muted block small'));
      else if (d.source === 'google') card.append(text('span', 'Listed on Google Maps · please call to confirm', 'muted block small'));

      const actions = document.createElement('div');
      actions.className = 'doctor-actions';
      if (d.phone) {
        const call = text('a', 'Call', 'btn');
        call.href = 'tel:' + d.phone.replace(/[^+\d]/g, '');
        actions.append(call);
      }
      if (d.whatsapp_url) {
        const wa = text('a', 'WhatsApp', 'btn');
        wa.href = d.whatsapp_url;
        wa.target = '_blank';
        wa.rel = 'noopener noreferrer';
        actions.append(wa);
      }
      if (d.website) {
        const web = text('a', 'Website', 'btn');
        web.href = d.website;
        web.target = '_blank';
        web.rel = 'noopener noreferrer';
        actions.append(web);
      }
      const dir = text('a', 'Directions', 'btn');
      dir.href = d.maps_url;
      dir.target = '_blank';
      dir.rel = 'noopener noreferrer';
      actions.append(dir);
      card.append(actions);
      results.append(card);
    }
  }

  async function search(params) {
    statusEl.textContent = 'Searching…';
    const headers = { 'Content-Type': 'application/json' };
    const csrf = document.querySelector('meta[name=csrf-token]');
    if (csrf) headers['X-CSRF-Token'] = csrf.content;
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers,
        body: JSON.stringify({ ...params, specialty: specialty.value || null }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(res.status === 429 ? (data.error || 'Too many searches. Please try again later.') : (data.error || 'Search failed.'));
      statusEl.textContent = (data.notice ? data.notice + ' ' : '') +
        'Distances are straight-line estimates. Please call ahead to confirm details.';
      render(data.results);
    } catch (err) {
      statusEl.textContent = err.message;
    }
  }

  btn.addEventListener('click', () => {
    if (area.value) return search({ area: area.value });
    if (!navigator.geolocation) {
      statusEl.textContent = 'Location is not available. Please choose your area.';
      return;
    }
    statusEl.textContent = 'Asking your device for its location…';
    navigator.geolocation.getCurrentPosition(
      pos => search({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
      () => { statusEl.textContent = 'Could not get your location. Please choose your area instead.'; },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 },
    );
  });
})();
