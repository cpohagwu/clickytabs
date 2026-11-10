import '@/styles/page.css';

import { mount } from 'svelte';

import { APP_NAME } from '@/lib/brand';

import Consent from './Consent.svelte';

document.title = `${APP_NAME}: allow a browser task?`;
mount(Consent, { target: document.getElementById('app')! });
