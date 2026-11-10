import '@/styles/page.css';

import { mount } from 'svelte';

import { APP_NAME } from '@/lib/brand';

import Popup from './Popup.svelte';

document.title = APP_NAME;
mount(Popup, { target: document.getElementById('app')! });
