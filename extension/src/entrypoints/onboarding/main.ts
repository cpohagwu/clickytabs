import '@/styles/page.css';

import { mount } from 'svelte';

import { APP_NAME } from '@/lib/brand';

import Onboarding from './Onboarding.svelte';

document.title = `Welcome to ${APP_NAME}`;
mount(Onboarding, { target: document.getElementById('app')! });
